"""Read-only terminal/session insight for discovered ATC workers.

Uses Mac `ps` tty + parent walk + optional cheap cwd via batched `lsof`.
Never signals processes.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# UUID after Claude CLI --resume
_RESUME_RE = re.compile(
    r"--resume(?:\s+|=)([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)

# Nearest parent app preference (first match while walking self → root wins
# among these; walk continues only if nothing matched yet).
_APP_RULES: list[tuple[str, str]] = [
    ("terminal.app", "Terminal.app"),
    ("iterm", "iTerm"),
    ("warp.app", "Warp"),
    ("alacritty", "Alacritty"),
    ("kitty.app", "kitty"),
    ("hyper.app", "Hyper"),
    ("claude.app", "Claude.app"),
]


@dataclass(frozen=True)
class SessionContext:
    tty: str | None = None
    app: str | None = None
    session_id: str | None = None
    cwd: str | None = None
    hint: str | None = None


def normalize_tty(tty: str | None) -> str | None:
    if not tty:
        return None
    t = tty.strip()
    if not t or t in {"?", "??", "-", " -"}:
        return None
    return t


def parse_claude_resume(command: str) -> str | None:
    if not command:
        return None
    m = _RESUME_RE.search(command)
    return m.group(1) if m else None


def classify_session_app(command: str) -> str | None:
    """Map a cmdline to a short parent-app / launcher label."""
    if not command:
        return None
    low = command.lower()
    for needle, label in _APP_RULES:
        if needle in low:
            return label
    # Exact token basename god / claude (not god-rt / god-watch)
    try:
        from backend.adapters.god_mode import _tokens, is_god_session_wrapper
    except Exception:
        return None
    if not is_god_session_wrapper(command):
        return None
    for t in _tokens(command):
        if not t or t.startswith("-"):
            continue
        name = Path(t).name.lower()
        if name == "god":
            return "god launcher"
        if name == "claude":
            return "Claude CLI"
    return None


def format_session_hint(
    tty: str | None,
    app: str | None,
    session_id: str | None,
    cwd: str | None = None,
) -> str | None:
    bits: list[str] = []
    if tty:
        bits.append(tty)
    if app:
        bits.append(app)
    if session_id:
        bits.append(f"resume {session_id[:8]}…")
    if not bits and cwd:
        bits.append(Path(cwd).name)
    return " · ".join(bits) if bits else None


def enrich_session(
    *,
    pid: int,
    ppid: int,
    tty: str | None,
    command: str,
    by_pid: dict[int, object],
    cwd_map: dict[int, str] | None = None,
) -> SessionContext:
    """Walk parents for app / --resume; attach tty + optional cwd."""
    tty_n = normalize_tty(tty)
    resume = parse_claude_resume(command)
    app = classify_session_app(command)

    # Parent walk (cycle-safe). Prefer first GUI/app label found nearest self.
    seen: set[int] = {pid}
    cur_ppid = ppid
    while cur_ppid and cur_ppid not in seen:
        seen.add(cur_ppid)
        parent = by_pid.get(cur_ppid)
        if parent is None:
            break
        pcmd = getattr(parent, "command", "") or ""
        if resume is None:
            resume = parse_claude_resume(pcmd)
        if app is None:
            app = classify_session_app(pcmd)
        cur_ppid = getattr(parent, "ppid", 0) or 0

    cwd = None
    if cwd_map is not None:
        cwd = cwd_map.get(pid)
    hint = format_session_hint(tty_n, app, resume, cwd)
    return SessionContext(tty=tty_n, app=app, session_id=resume, cwd=cwd, hint=hint)


def read_cwds_cheap(pids: Iterable[int], *, timeout: float = 1.5) -> dict[int, str]:
    """One batched `lsof` for cwd of selected PIDs. Best-effort / read-only."""
    uniq = sorted({int(p) for p in pids if p and int(p) > 0})
    if not uniq:
        return {}
    # Cap to keep discovery snappy
    uniq = uniq[:40]
    try:
        out = subprocess.check_output(
            ["lsof", "-a", "-d", "cwd", "-Fn", "-p", ",".join(str(p) for p in uniq)],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return {}
    result: dict[int, str] = {}
    cur: int | None = None
    for line in out.splitlines():
        if not line:
            continue
        if line.startswith("p") and line[1:].isdigit():
            cur = int(line[1:])
        elif line.startswith("n") and cur is not None:
            path = line[1:].strip()
            if path and path != "/":
                result[cur] = path
    return result


def spoken_session_answer(worker: dict) -> str:
    """Short TTS-friendly answer for where/session questions."""
    name = worker.get("name") or worker.get("id") or "Worker"
    tty = worker.get("session_tty")
    app = worker.get("session_app")
    sid = worker.get("session_id")
    cwd = worker.get("session_cwd")
    hint = worker.get("session_hint")
    source = (worker.get("source") or "").lower()

    if source == "demo" and not tty:
        return f"{name} is an ATC demo worker (no terminal)."
    if hint and tty and app:
        msg = f"{name} is on {tty} in {app}."
    elif tty and app:
        msg = f"{name} is on {tty} in {app}."
    elif tty:
        msg = f"{name} is on {tty}."
    elif app:
        msg = f"{name} is under {app}."
    elif hint:
        msg = f"{name}: {hint}."
    else:
        msg = f"{name} has no terminal session metadata."
    if sid:
        msg = msg.rstrip(".") + f" Claude resume {sid[:8]}."
    elif cwd and not tty:
        msg = msg.rstrip(".") + f" cwd {Path(cwd).name}."
    return msg
