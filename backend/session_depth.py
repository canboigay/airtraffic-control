"""Session depth for tower UI: idle/stale, left-off transcripts, steer-prompt.

Read-only for status/transcripts. Steer writes an inbox always; tty inject
only on explicit operator send (never auto).
"""

from __future__ import annotations

import fcntl
import json
import os
import platform
import re
import subprocess
import termios
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DEFAULT_STACK = "/Users/simeong/local-claude-offline-stack"
DEFAULT_HOME = str(Path.home())

# Quiet process: sleeping / idle states with low CPU → idle badge.
# Transcript (or last-answer) older than STALE_SEC while quiet → stale.
IDLE_CPU_MAX = float(os.environ.get("ATC_IDLE_CPU_MAX", "1.0"))
STALE_SEC = float(os.environ.get("ATC_STALE_SEC", "600"))  # 10 min
LEFT_OFF_LINES = 8
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_SLEEP_STATES = frozenset({"S", "I", "U", "W"})  # interruptible / uninterruptible sleep


@dataclass
class LeftOff:
    lines: list[str] = field(default_factory=list)
    source_path: str | None = None
    source_kind: str | None = None  # claude_jsonl | god_last_answer | agy | grok | none
    session_id: str | None = None
    mtime: float | None = None
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "lines": list(self.lines),
            "source_path": self.source_path,
            "source_kind": self.source_kind,
            "session_id": self.session_id,
            "mtime": self.mtime,
            "note": self.note,
            "line_count": len(self.lines),
        }

    def preview(self, max_len: int = 140) -> str | None:
        """One-line card blurb for list workers (None when nothing useful)."""
        text = ""
        if self.lines:
            text = str(self.lines[-1] or "").strip()
        elif self.note and self.note not in {"no transcript yet", "no session id"}:
            text = str(self.note).strip()
        if not text:
            return None
        # Collapse whitespace for card meta
        text = " ".join(text.split())
        if len(text) > max_len:
            text = text[: max_len - 1].rstrip() + "…"
        return text


@dataclass
class SteerDelivery:
    ok: bool
    method: str  # inbox | tty | inbox+tty
    inbox_path: str | None = None
    tty: str | None = None
    summary: str = ""
    error: str | None = None
    worker_id: str | None = None
    session_id: str | None = None
    submit: str | None = None  # e.g. "cr+tiocsti" / "cr+applescript-terminal"
    delivered: bool = False  # True only when prompt+Enter reached the live tty
    session_app: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "method": self.method,
            "inbox_path": self.inbox_path,
            "tty": self.tty,
            "summary": self.summary,
            "error": self.error,
            "worker_id": self.worker_id,
            "session_id": self.session_id,
            "submit": self.submit,
            "delivered": self.delivered,
            "session_app": self.session_app,
        }


def _now() -> float:
    return time.time()


def is_sleeping_state(state: str | None) -> bool:
    if not state:
        return False
    return state[0].upper() in _SLEEP_STATES


def is_stopped_state(state: str | None) -> bool:
    return bool(state) and state[0].upper() == "T"


def compute_activity_status(
    *,
    state: str | None,
    cpu: float | None,
    source: str | None,
    transcript_mtime: float | None = None,
    now: float | None = None,
    idle_cpu_max: float = IDLE_CPU_MAX,
    stale_sec: float = STALE_SEC,
) -> str:
    """Return paused | idle | stale | running for session/cli; else paused|running.

    Idle/stale only applied to source in {session, cli}. Other workers stay
    running/paused so demo/god workloads are unchanged.
    """
    if is_stopped_state(state):
        return "paused"
    src = (source or "").strip().lower()
    if src not in {"session", "cli"}:
        return "running"
    cpu_v = 0.0 if cpu is None else float(cpu)
    quiet = is_sleeping_state(state) and cpu_v <= idle_cpu_max
    if not quiet:
        # Runnable / high CPU — treat as running even if transcript is old
        return "running"
    ts = now if now is not None else _now()
    if transcript_mtime is not None and (ts - float(transcript_mtime)) >= stale_sec:
        return "stale"
    return "idle"


def find_claude_jsonl(
    session_id: str,
    *,
    home: str | Path | None = None,
) -> Path | None:
    """Prefer ~/.claude/projects/**/<session_id>.jsonl."""
    if not session_id or not _UUID_RE.fullmatch(session_id.strip()):
        return None
    sid = session_id.strip()
    root = Path(home or os.environ.get("HOME") or DEFAULT_HOME) / ".claude" / "projects"
    if not root.is_dir():
        return None
    # Exact filename match anywhere under projects
    hits = sorted(
        root.glob(f"**/{sid}.jsonl"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )
    for h in hits:
        if h.is_file():
            return h
    # Directory named after session with jsonl inside
    for d in root.glob(f"**/{sid}"):
        if d.is_dir():
            cand = d / f"{sid}.jsonl"
            if cand.is_file():
                return cand
    return None


def god_last_answer_path(
    session_id: str,
    *,
    stack: str | Path | None = None,
) -> Path | None:
    if not session_id:
        return None
    base = Path(stack or os.environ.get("GOD_STACK") or DEFAULT_STACK) / ".god-last-answer"
    p = base / f"session-{session_id.strip()}.json"
    return p if p.is_file() else None


def _message_text(obj: dict[str, Any]) -> str | None:
    """Pull human-readable text from a Claude jsonl record."""
    typ = (obj.get("type") or "").lower()
    msg = obj.get("message")
    role = None
    content = None
    if isinstance(msg, dict):
        role = (msg.get("role") or typ or "").lower()
        content = msg.get("content")
    elif typ in {"user", "assistant"}:
        role = typ
        content = obj.get("content")
    else:
        return None
    if role not in {"user", "assistant", "human"}:
        # Skip tool/progress/system noise
        if typ not in {"user", "assistant"}:
            return None
        role = typ
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        bits: list[str] = []
        for block in content:
            if isinstance(block, str):
                bits.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text" and block.get("text"):
                    bits.append(str(block["text"]))
                elif block.get("type") == "tool_use":
                    name = block.get("name") or "tool"
                    bits.append(f"[tool_use {name}]")
                elif block.get("type") == "tool_result":
                    continue  # skip bulky tool results
        text = "\n".join(bits)
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if not text:
        return None
    if len(text) > 600:
        text = text[:597] + "..."
    label = "you" if role in {"user", "human"} else "assistant"
    return f"{label}: {text}"


def _tail_jsonl_texts(path: Path, *, limit: int = LEFT_OFF_LINES, max_bytes: int = 512_000) -> list[str]:
    """Read trailing records from a jsonl and extract last N text lines."""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    try:
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # drop partial
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        got = _message_text(obj)
        if got:
            out.append(got)
    return out[-max(1, limit) :]


def read_claude_left_off(
    session_id: str,
    *,
    stack: str | Path | None = None,
    home: str | Path | None = None,
    lines: int = LEFT_OFF_LINES,
) -> LeftOff:
    sid = (session_id or "").strip()
    if not sid:
        return LeftOff(note="no session id", source_kind="none")
    jsonl = find_claude_jsonl(sid, home=home)
    last = god_last_answer_path(sid, stack=stack)
    mtime: float | None = None
    if jsonl is not None:
        try:
            mtime = jsonl.stat().st_mtime
        except OSError:
            mtime = None
        texts = _tail_jsonl_texts(jsonl, limit=lines)
        if texts:
            note = None
            if last is not None:
                try:
                    meta = json.loads(last.read_text(encoding="utf-8"))
                    model = meta.get("response_model") or meta.get("routed")
                    ts = meta.get("ts")
                    if model or ts:
                        note = f"god-last-answer: {model or '?'} @ {ts or '?'}"
                except (OSError, json.JSONDecodeError):
                    pass
            return LeftOff(
                lines=texts,
                source_path=str(jsonl),
                source_kind="claude_jsonl",
                session_id=sid,
                mtime=mtime,
                note=note,
            )
    if last is not None:
        try:
            mtime = last.stat().st_mtime
            meta = json.loads(last.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        line = (
            f"last answer via {meta.get('response_model') or meta.get('routed') or '?'} "
            f"at {meta.get('ts') or '?'}"
        )
        return LeftOff(
            lines=[line],
            source_path=str(last),
            source_kind="god_last_answer",
            session_id=sid,
            mtime=mtime,
            note="no claude jsonl transcript; showing god-last-answer metadata",
        )
    return LeftOff(
        lines=[],
        session_id=sid,
        source_kind="none",
        note="no transcript yet",
    )


def _agy_brain_root(home: str | Path | None = None) -> Path:
    return Path(home or os.environ.get("HOME") or DEFAULT_HOME) / ".gemini" / "antigravity-cli"


def read_agy_left_off(
    *,
    pid: int | None = None,
    cwd: str | None = None,
    home: str | Path | None = None,
    lines: int = LEFT_OFF_LINES,
) -> LeftOff:
    root = _agy_brain_root(home)
    if not root.is_dir():
        return LeftOff(note="no transcript yet", source_kind="none")
    cand: Path | None = None
    # cwd under brain/<uuid>
    if cwd:
        m = re.search(r"/brain/([0-9a-fA-F-]{36})", cwd.replace("\\", "/"))
        if m:
            t = root / "brain" / m.group(1) / ".system_generated" / "logs" / "transcript.jsonl"
            if t.is_file():
                cand = t
    # crash log for this pid is evidence of an agy session, but does not
    # reliably map to a brain dir — without cwd, stay honest.
    if cand is None:
        return LeftOff(note="no transcript yet", source_kind="none")
    if not cand.is_file():
        return LeftOff(note="no transcript yet", source_kind="none")
    try:
        mtime = cand.stat().st_mtime
    except OSError:
        mtime = None
    # agy transcript may be plain text or jsonl — try both
    texts = _tail_jsonl_texts(cand, limit=lines)
    if not texts:
        try:
            raw = cand.read_text(encoding="utf-8", errors="replace").splitlines()
            texts = [ln.strip() for ln in raw if ln.strip()][-lines:]
        except OSError:
            texts = []
    if not texts:
        return LeftOff(
            lines=[],
            source_path=str(cand),
            source_kind="agy",
            mtime=mtime,
            note="no transcript yet",
        )
    return LeftOff(
        lines=texts,
        source_path=str(cand),
        source_kind="agy",
        mtime=mtime,
    )


def read_grok_left_off(
    *,
    pid: int | None = None,
    home: str | Path | None = None,
    lines: int = LEFT_OFF_LINES,
) -> LeftOff:
    """Best-effort: newest ~/.grok/memtrace/*.jsonl or honest empty."""
    base = Path(home or os.environ.get("HOME") or DEFAULT_HOME) / ".grok"
    mem = base / "memtrace"
    if not mem.is_dir():
        return LeftOff(note="no transcript yet", source_kind="none")
    hits = sorted(
        mem.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )
    # Prefer filename containing pid
    chosen = None
    if pid:
        for h in hits:
            if str(pid) in h.name:
                chosen = h
                break
    if chosen is None and hits:
        # Too ambiguous to claim as this worker — be honest
        return LeftOff(
            note="no transcript yet (grok memtrace not mapped to this pid)",
            source_kind="none",
        )
    if chosen is None:
        return LeftOff(note="no transcript yet", source_kind="none")
    try:
        mtime = chosen.stat().st_mtime
    except OSError:
        mtime = None
    texts = _tail_jsonl_texts(chosen, limit=lines)
    if not texts:
        return LeftOff(
            lines=[],
            source_path=str(chosen),
            source_kind="grok",
            mtime=mtime,
            note="no transcript yet",
        )
    return LeftOff(
        lines=texts,
        source_path=str(chosen),
        source_kind="grok",
        mtime=mtime,
    )


def resolve_left_off(
    *,
    source: str | None,
    slug: str | None = None,
    session_id: str | None = None,
    pid: int | None = None,
    cwd: str | None = None,
    stack: str | Path | None = None,
    home: str | Path | None = None,
    lines: int = LEFT_OFF_LINES,
) -> LeftOff:
    """Pick the right transcript source for a worker."""
    src = (source or "").lower()
    slug_l = (slug or "").lower()
    if session_id:
        return read_claude_left_off(session_id, stack=stack, home=home, lines=lines)
    if src == "cli" or slug_l in {"agy-cli", "grok-cli", "gemini-cli"}:
        if "agy" in slug_l or "antigravity" in slug_l:
            return read_agy_left_off(pid=pid, cwd=cwd, home=home, lines=lines)
        if "grok" in slug_l:
            return read_grok_left_off(pid=pid, home=home, lines=lines)
        return LeftOff(note="no transcript yet", source_kind="none")
    if src == "session":
        return LeftOff(note="no transcript yet", source_kind="none")
    return LeftOff(note="no transcript yet", source_kind="none")


def child_or_self_session_id(
    pid: int,
    command: str,
    by_pid: dict[int, Any],
    *,
    parse_resume,
) -> str | None:
    """Session id from self cmdline, else first descendant with Claude --resume."""
    own = parse_resume(command or "")
    if own:
        return own
    # BFS children
    children: dict[int, list[int]] = {}
    for p in by_pid.values():
        children.setdefault(getattr(p, "ppid", 0) or 0, []).append(getattr(p, "pid", 0))
    stack = list(children.get(pid, []))
    seen: set[int] = {pid}
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        proc = by_pid.get(cur)
        if proc is None:
            continue
        got = parse_resume(getattr(proc, "command", "") or "")
        if got:
            return got
        stack.extend(children.get(cur, []))
    return None


def inbox_dir(*, stack: str | Path | None = None, root: Path | None = None) -> Path:
    """ATC steer inbox under the repo logs/ (safe, never auto-consumed into TTY)."""
    if root is not None:
        d = Path(root)
    else:
        # airtraffic-control/logs/steer-inbox
        d = Path(__file__).resolve().parents[1] / "logs" / "steer-inbox"
    d.mkdir(parents=True, exist_ok=True)
    return d

def write_steer_inbox(
    *,
    worker_id: str,
    prompt: str,
    session_id: str | None = None,
    tty: str | None = None,
    pid: int | None = None,
    source: str | None = None,
    inbox_root: Path | None = None,
    delivered: bool = False,
) -> Path:
    text = (prompt or "").strip()
    if not text:
        raise ValueError("empty steer prompt")
    dest = inbox_dir(root=inbox_root)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_wid = re.sub(r"[^a-zA-Z0-9._-]+", "_", worker_id)[:80]
    path = dest / f"{safe_wid}-{ts}.json"
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "worker_id": worker_id,
        "session_id": session_id,
        "tty": tty,
        "pid": pid,
        "source": source,
        "prompt": text,
        "delivered": bool(delivered),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    # Also append to a per-worker jsonl for easy tailing
    jpath = dest / f"{safe_wid}.jsonl"
    with jpath.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return path


def mark_inbox_delivered(path: Path | str, *, submit: str | None = None) -> None:
    """Flip delivered:true on the inbox JSON after a successful tty submit."""
    p = Path(path)
    if not p.is_file():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return
    data["delivered"] = True
    if submit:
        data["submit"] = submit
    data["delivered_at"] = datetime.now(timezone.utc).isoformat()
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _sanitize_inject_text(prompt: str) -> str:
    text = (prompt or "").strip()
    if not text:
        raise ValueError("empty steer prompt")
    # Disallow control chars except tab
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", text):
        raise ValueError("steer prompt contains control characters")
    if "\n" in text or "\r" in text:
        # Single-line inject only (avoid paste bombs)
        text = re.sub(r"[\r\n]+", " ", text).strip()
    return text


def _normalize_tty_name(tty: str) -> str:
    t = (tty or "").strip()
    if not t or t in {"?", "??", "-"}:
        raise ValueError("no tty for inject")
    if t.startswith("/dev/"):
        t = t[len("/dev/") :]
    return t


def _applescript_quote(s: str) -> str:
    """Quote a Python string as an AppleScript string literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def inject_tty_tiocsti(tty: str, prompt: str) -> str:
    """Inject prompt as keystrokes into /dev/<tty>, then submit with CR (Enter).

    Uses TIOCSTI so characters enter the TTY *input* queue (as if typed), not
    merely written to the display. Submit is ASCII CR (\\r) — the Enter key —
    because raw/TUI readers (agy, ink, etc.) often ignore a lone LF write and
    leave the line half-entered.

    Does not change termios/line discipline; only queues input chars. Explicit
    send only — never auto.

    Returns submit tag "cr+tiocsti".
    """
    t = _normalize_tty_name(tty)
    text = _sanitize_inject_text(prompt)
    dev = Path("/dev") / t
    if not dev.exists():
        raise FileNotFoundError(f"tty device missing: {dev}")
    # Enter = CR. Do not use LF-only: paste-without-submit on raw TUIs.
    payload = text + "\r"
    tiocsti = getattr(termios, "TIOCSTI", None)
    if tiocsti is None:
        raise RuntimeError("TIOCSTI unavailable — cannot inject keystrokes safely")
    # RDWR | O_NOCTTY — need a live fd for ioctl; do not take controlling tty
    fd = os.open(str(dev), os.O_RDWR | getattr(os, "O_NOCTTY", 0))
    try:
        for ch in payload:
            try:
                fcntl.ioctl(fd, tiocsti, ch)
            except OSError as e:
                raise RuntimeError(
                    f"TIOCSTI inject failed on {t} (partial input possible): {e}"
                ) from e
    finally:
        os.close(fd)
    return "cr+tiocsti"


def inject_tty_prompt(tty: str, prompt: str) -> str:
    """Backward-compatible TIOCSTI inject (returns submit tag)."""
    return inject_tty_tiocsti(tty, prompt)


def _run_osascript(script: str, *, timeout: float = 12.0) -> str:
    """Run AppleScript. HARD BAN: never open new Terminal/iTerm windows/tabs.

    Scripts must only select an existing tab/session by tty. Reject any script
    that uses Terminal `do script` or `make new` (those spawn windows).
    """
    low = (script or "").lower()
    if "do script" in low:
        raise RuntimeError(
            "refusing AppleScript that uses 'do script' (would open a new Terminal window)"
        )
    if "make new" in low:
        raise RuntimeError(
            "refusing AppleScript that uses 'make new' (would open a new window/tab)"
        )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as e:
        raise RuntimeError("osascript not found") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("osascript timed out") from e
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        raise RuntimeError(err)
    return (proc.stdout or "").strip()


_TERMINAL_APPLESCRIPT = '''
set targetTTY to __Q_TTY__
set targetDev to __Q_DEV__
set typed to __Q_TEXT__
tell application "Terminal"
  set matched to false
  set winCount to count of windows
  repeat with wi from 1 to winCount
    set w to window wi
    set tabCount to count of tabs of w
    repeat with ti from 1 to tabCount
      set tabTTY to (tty of tab ti of w) as text
      if tabTTY is targetDev or tabTTY is targetTTY or tabTTY ends with targetTTY then
        set selected of tab ti of w to true
        set frontmost of w to true
        set index of w to 1
        set matched to true
        exit repeat
      end if
    end repeat
    if matched then exit repeat
  end repeat
  if not matched then error "no Terminal.app tab for " & targetTTY
  activate
end tell
delay 0.15
tell application "System Events"
  if not (exists process "Terminal") then error "Terminal process missing"
  tell process "Terminal"
    set frontmost to true
    keystroke typed
    keystroke return
  end tell
end tell
'''


_ITERM_APPLESCRIPT = '''
set targetTTY to __Q_TTY__
set targetDev to __Q_DEV__
set typed to __Q_TEXT__
tell application "iTerm"
  set matched to false
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        set sessTTY to ""
        try
          set sessTTY to (tty of s) as text
        end try
        if sessTTY is targetDev or sessTTY is targetTTY or sessTTY ends with targetTTY then
          select t
          tell s
            write text typed
          end tell
          set matched to true
          exit repeat
        end if
      end repeat
      if matched then exit repeat
    end repeat
    if matched then exit repeat
  end repeat
  if not matched then error "no iTerm session for " & targetTTY
  activate
end tell
'''


def inject_tty_applescript(
    tty: str,
    prompt: str,
    *,
    app: str | None = None,
) -> str:
    """macOS fallback: type prompt + Return into an EXISTING Terminal.app / iTerm tab.

    Prefer Terminal.app when app is Terminal.app or unknown (agy/grok/god default).
    iTerm uses native `write text` (includes newline). Terminal.app selects the
    matching tab then System Events keystroke + return.

    NEVER opens a new window/tab (`do script` / `make new` are banned). If no
    existing tab matches the tty, raise — caller keeps inbox and surfaces error.

    Requires Accessibility for System Events keystrokes (Terminal path).
    Returns submit tag like "cr+applescript-terminal".
    """
    if platform.system() != "Darwin":
        raise RuntimeError("AppleScript inject only available on macOS")
    t = _normalize_tty_name(tty)
    text = _sanitize_inject_text(prompt)
    app_l = (app or "").strip().lower()
    prefer_iterm = "iterm" in app_l
    if prefer_iterm:
        order = ("iterm", "terminal")
    else:
        order = ("terminal", "iterm")

    errors: list[str] = []
    for kind in order:
        try:
            if kind == "terminal":
                _inject_terminal_applescript(t, text)
                return "cr+applescript-terminal"
            _inject_iterm_applescript(t, text)
            return "cr+applescript-iterm"
        except Exception as e:
            errors.append(f"{kind}: {e}")
            continue
    raise RuntimeError("; ".join(errors) or "AppleScript inject failed")


def _inject_terminal_applescript(tty_name: str, text: str) -> None:
    """Activate Terminal.app tab whose tty matches, keystroke text, Return."""
    script = (
        _TERMINAL_APPLESCRIPT
        .replace("__Q_TTY__", _applescript_quote(tty_name))
        .replace("__Q_DEV__", _applescript_quote(f"/dev/{tty_name}"))
        .replace("__Q_TEXT__", _applescript_quote(text))
    )
    _run_osascript(script)


def _inject_iterm_applescript(tty_name: str, text: str) -> None:
    """Find iTerm session by tty and write text (includes newline)."""
    script = (
        _ITERM_APPLESCRIPT
        .replace("__Q_TTY__", _applescript_quote(tty_name))
        .replace("__Q_DEV__", _applescript_quote(f"/dev/{tty_name}"))
        .replace("__Q_TEXT__", _applescript_quote(text))
    )
    _run_osascript(script)


def deliver_steer_prompt(
    *,
    worker_id: str,
    prompt: str,
    session_id: str | None = None,
    tty: str | None = None,
    pid: int | None = None,
    source: str | None = None,
    method: str = "auto",
    inbox_root: Path | None = None,
    inject_fn=None,
    applescript_fn=None,
    session_app: str | None = None,
) -> SteerDelivery:
    """Always write inbox. TTY inject when method is tty/auto and tty present.

    Order on macOS (cli/session with known tty):
      1) TIOCSTI (+ CR)
      2) Terminal.app / iTerm AppleScript keystroke + Return

    method:
      - inbox: inbox only (safe)
      - tty: inbox + tty (explicit)
      - auto: inbox + tty when tty is known (UI/voice Send is always explicit)

    delivered=True only when prompt+Enter reached the live terminal. Inbox-only
    after a failed inject returns ok=False so UI/API never pretend success.
    """
    text = (prompt or "").strip()
    if not text:
        return SteerDelivery(
            ok=False,
            method=method,
            error="empty steer prompt",
            worker_id=worker_id,
            session_id=session_id,
            delivered=False,
            session_app=session_app,
        )
    try:
        path = write_steer_inbox(
            worker_id=worker_id,
            prompt=text,
            session_id=session_id,
            tty=tty,
            pid=pid,
            source=source,
            inbox_root=inbox_root,
            delivered=False,
        )
    except Exception as e:
        return SteerDelivery(
            ok=False,
            method="inbox",
            error=f"inbox write failed: {e}",
            worker_id=worker_id,
            session_id=session_id,
            delivered=False,
            session_app=session_app,
        )

    m = (method or "auto").strip().lower()
    want_tty = m in {"tty", "auto", "inbox+tty"}
    tty_n = (tty or "").strip() or None
    if want_tty and tty_n:
        errors: list[str] = []
        # 1) TIOCSTI (or injected test double)
        try:
            fn = inject_fn or inject_tty_tiocsti
            submit = fn(tty_n, text)
            if submit is None:
                submit = "cr+tiocsti"
            mark_inbox_delivered(path, submit=str(submit))
            return SteerDelivery(
                ok=True,
                method="inbox+tty",
                inbox_path=str(path),
                tty=tty_n,
                summary=f"prompt+Enter delivered to {tty_n} via {submit} (inbox {path.name})",
                worker_id=worker_id,
                session_id=session_id,
                submit=str(submit),
                delivered=True,
                session_app=session_app,
            )
        except Exception as e:
            errors.append(f"tiocsti: {e}")

        # 2) macOS AppleScript fallback (Terminal.app / iTerm)
        if platform.system() == "Darwin" or applescript_fn is not None:
            try:
                as_fn = applescript_fn or inject_tty_applescript
                submit = as_fn(tty_n, text, app=session_app)
                if submit is None:
                    submit = "cr+applescript"
                mark_inbox_delivered(path, submit=str(submit))
                return SteerDelivery(
                    ok=True,
                    method="inbox+tty",
                    inbox_path=str(path),
                    tty=tty_n,
                    summary=(
                        f"prompt+Enter delivered to {tty_n} via {submit} "
                        f"(TIOCSTI failed; inbox {path.name})"
                    ),
                    worker_id=worker_id,
                    session_id=session_id,
                    submit=str(submit),
                    delivered=True,
                    session_app=session_app,
                )
            except Exception as e:
                errors.append(f"applescript: {e}")

        err_msg = "tty inject failed: " + " | ".join(errors)
        return SteerDelivery(
            ok=False,  # inbox kept, but do NOT pretend terminal delivery
            method="inbox",
            inbox_path=str(path),
            tty=tty_n,
            summary=(
                f"NOT submitted to terminal ({tty_n}). Inbox only ({path.name}). {err_msg}"
            ),
            error=err_msg,
            worker_id=worker_id,
            session_id=session_id,
            delivered=False,
            session_app=session_app,
        )
    return SteerDelivery(
        ok=True,
        method="inbox",
        inbox_path=str(path),
        tty=tty_n,
        summary=f"prompt queued in inbox ({path.name})"
        + ("" if tty_n else " — no tty to inject"),
        worker_id=worker_id,
        session_id=session_id,
        delivered=False,
        session_app=session_app,
    )
