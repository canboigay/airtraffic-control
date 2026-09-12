"""Local God Mode adapter — discover and control real user processes.

Discovers Simeon's God Mode stack workloads, optional ATC demo workers,
and terminal Grok CLI / Gemini CLI sessions (source=cli), and exposes
pause (SIGSTOP on the listed PID only) / resume (SIGCONT tree) /
kill (TERM then KILL) / redirect (label + real god-rt quiet steer for God RT) /
restart (resume-if-paused only for discovered PIDs).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Iterable

from backend.registry import Worker, WorkerStatus, tail_log_file
from backend.adapters.god_rt_steer import (
    is_god_rt_worker,
    run_quiet_steer,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STACK = "/Users/simeong/local-claude-offline-stack"
DEFAULT_USER = "simeong"
TARGETS_PATH = ROOT / "logs" / "god-targets.json"

KEYWORDS = ("god-rt", "campaign-harness", "csuper", "god-watch", "mcp_hands")
DEMO_SCRIPTS = {
    "log_spam.py": ("log-spam", "Log Spam"),
    "fake_build.py": ("fake-build", "Fake Build"),
    "fake_research.py": ("fake-research", "Fake Research"),
}
SLUG_NAMES = {
    "god-rt": "God RT",
    "campaign-harness": "Campaign Harness",
    "csuper": "Campaign Supervisor",
    "god-watch": "God Watch",
    "mcp-hands": "MCP Hands",
    "log-spam": "Log Spam",
    "fake-build": "Fake Build",
    "fake-research": "Fake Research",
    "grok-cli": "Grok CLI",
    "gemini-cli": "Gemini CLI",
    "agy-cli": "Antigravity",
}
INTERPRETERS = {
    "python",
    "python3",
    "python3.11",
    "python3.12",
    "python3.13",
    "python3.14",
    "zsh",
    "bash",
    "sh",
    "node",
    "nodejs",
    "uv",
}
SCANNER_BINS = {
    "rg",
    "grep",
    "egrep",
    "fgrep",
    "ag",
    "find",
    "lsof",
    "ps",
    "cat",
    "head",
    "tail",
    "less",
    "more",
    "awk",
    "sed",
    "wc",
    "strings",
}
PROTECTED_PORTS = (8765, 8088, 8089)
DENY_SUBSTR = (
    "/applications/claude.app",
    "claude helper",
    "/applications/cursor.app",
    "cursor helper",
    "cursoruiviewservice",
    "grok bot",
    "/applications/grok bot.app",
    "loginwindow",
)
DENY_PREFIXES = (
    "/system/library/",
    "/usr/libexec/",
    "/sbin/",
    "/usr/sbin/",
    "/library/apple/",
)
# Live TUI session wrappers (basename only). god-rt / god-watch / god_gate.py
# are NOT these — they keep their own names.
SESSION_WRAPPER_NAMES = frozenset({"god", "claude"})
# Terminal AI CLIs (exact basename only — never ngrok/progrok/Grok Bot.app).
CLI_BINS = frozenset({"grok", "gemini", "agy", "antigravity"})
CLI_SLUGS = {
    "grok": "grok-cli",
    "gemini": "gemini-cli",
    "agy": "agy-cli",
    "antigravity": "agy-cli",
}
SCRIPT_EXTS = {".py", ".zsh", ".sh", ".js", ".mjs", ".ts", ".cjs", ""}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(value: str) -> str:
    return value.strip().lower().replace(" ", "-").replace("_", "-")


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    state: str
    command: str
    user: str = DEFAULT_USER
    tty: str = "?"
    cpu: float = 0.0  # ps %cpu

    @property
    def stopped(self) -> bool:
        return bool(self.state) and self.state[0].upper() == "T"


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def _argv0_name(command: str) -> str:
    toks = _tokens(command)
    if not toks:
        return ""
    return Path(toks[0]).name.lower()


def _is_cursor_wrapper(command: str) -> bool:
    return "dump_zsh_state" in command or "__CURSOR_SANDBOX" in command


def _is_scanner(command: str) -> bool:
    if _is_cursor_wrapper(command):
        return True
    name = _argv0_name(command)
    if name in SCANNER_BINS:
        return True
    # `zsh -c 'rg …'` / `bash -c 'grep …'` used by agent shells
    toks = _tokens(command)
    if len(toks) >= 3 and Path(toks[0]).name.lower() in {"zsh", "bash", "sh"} and toks[1] in {"-c", "-lc"}:
        inner = toks[2]
        inner0 = inner.strip().split(None, 1)[0] if inner.strip() else ""
        if Path(inner0).name.lower() in SCANNER_BINS:
            return True
    return False


def is_god_session_wrapper(command: str) -> bool:
    """True for live `zsh …/god` launchers and `claude` CLI god sessions.

    Matches token basename only (`god`, `claude`) so `god-rt`, `god-watch`,
    `god_gate.py`, and `god-mcp-hands` stay discoverable as workloads.
    """
    toks = _tokens(command)
    if not toks:
        return False
    for t in toks:
        if not t or t.startswith("-"):
            continue
        # `--settings=/path/claude` must not count; already skipped by '-'
        name = Path(t).name.lower()
        if name in SESSION_WRAPPER_NAMES:
            return True
    return False


def descendant_pids(procs: Iterable[Proc], root_pid: int) -> list[int]:
    """Descendant PIDs of root_pid (root itself excluded), cycle-safe."""
    by_parent: dict[int, list[int]] = {}
    for p in procs:
        by_parent.setdefault(p.ppid, []).append(p.pid)
    out: list[int] = []
    stack = list(by_parent.get(root_pid, []))
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen or pid == root_pid:
            continue
        seen.add(pid)
        out.append(pid)
        stack.extend(by_parent.get(pid, []))
    return out



def is_mcp_hands_cmdline(command: str) -> bool:
    """True only for mcp_hands server / uv wrappers that clearly run mcp_hands."""
    low = (command or "").lower()
    if "mcp_hands" not in low and "mcp-hands" not in low:
        return False
    # Exact server module or path containing mcp_hands project/bin
    if "mcp_hands.server" in low or "-m mcp_hands" in low:
        return True
    if "mcp_hands" in low and ("uv run" in low or "/mcp_hands" in low):
        return True
    return False


def kill_target_pids(proc: "Proc", procs: Iterable["Proc"]) -> list[int]:
    """PIDs to signal on kill — never walk into god/claude ancestors.

    Hard rule (Simeon):
    - Default: listed PID only.
    - mcp_hands: listed `mcp_hands.server` PID, plus immediate uv/python parent
      ONLY when that parent's cmdline clearly mentions mcp_hands.
    - Never SIGTERM/SIGKILL `zsh …/god` or `claude --resume …` as collateral.
    - No bulk tree kill / no ancestor walk into session wrappers.
    """
    by_pid = {p.pid: p for p in procs}
    # Explicit kill of a god/claude session: that PID only (never children/parents).
    if is_god_session_wrapper(proc.command):
        return [proc.pid]
    targets: list[int] = [proc.pid]
    if is_mcp_hands_cmdline(proc.command):
        parent = by_pid.get(proc.ppid)
        if (
            parent is not None
            and is_mcp_hands_cmdline(parent.command)
            and not is_god_session_wrapper(parent.command)
            and not is_denied_cmdline(parent.command)
        ):
            targets.append(parent.pid)
    # Final filter: never signal protected wrappers even if mis-parented
    out: list[int] = []
    for pid in targets:
        p = by_pid.get(pid)
        if p is not None and (
            is_god_session_wrapper(p.command) or is_denied_cmdline(p.command)
        ):
            continue
        out.append(pid)
    # Always keep the leaf if it itself is a legitimate mcp/workload (not wrapper)
    if proc.pid not in out and not is_god_session_wrapper(proc.command) and not is_denied_cmdline(proc.command):
        out.insert(0, proc.pid)
    return list(dict.fromkeys(out))


def cont_target_pids(root_pid: int, procs: Iterable["Proc"]) -> list[int]:
    """SIGCONT targets: root + descendants, skipping god/claude wrappers."""
    by_pid = {p.pid: p for p in procs}
    out: list[int] = []
    for pid in [root_pid, *descendant_pids(procs, root_pid)]:
        p = by_pid.get(pid)
        if p is not None and (
            is_god_session_wrapper(p.command) or is_denied_cmdline(p.command)
        ):
            continue
        out.append(pid)
    if root_pid not in out:
        # Still CONT the root workload itself if it is not a wrapper
        root = by_pid.get(root_pid)
        if root is None or not (
            is_god_session_wrapper(root.command) or is_denied_cmdline(root.command)
        ):
            out.insert(0, root_pid)
    return list(dict.fromkeys(out))

def ancestor_is_protected_session(proc: Proc, procs: Iterable[Proc]) -> bool:
    """True if any ancestor is a god/claude wrapper or other denied session."""
    by_pid = {p.pid: p for p in procs}
    seen: set[int] = set()
    cur = by_pid.get(proc.ppid)
    while cur is not None and cur.pid not in seen:
        seen.add(cur.pid)
        if is_god_session_wrapper(cur.command) or is_denied_cmdline(cur.command):
            return True
        cur = by_pid.get(cur.ppid)
    return False


def is_denied_cmdline(command: str) -> bool:
    low = command.lower()
    if any(s in low for s in DENY_SUBSTR):
        return True
    if "loginwindow" in low:
        return True
    toks = _tokens(command)
    if toks:
        argv0 = toks[0]
        low0 = argv0.lower()
        if any(low0.startswith(p) for p in DENY_PREFIXES):
            return True
    # ATC's own API server (also covered by port 8765)
    if "uvicorn" in low and ("backend.main:app" in low or "airtraffic-control" in low):
        return True
    # god/claude TUI wrappers are LISTED as protected sessions (not denied here).
    # GUI apps (Claude.app / Cursor / Grok Bot) stay denied via DENY_SUBSTR above.
    return False


def _looks_like_script(path: str) -> bool:
    if not path or path.startswith("-"):
        return False
    name = Path(path).name
    if name.startswith("-"):
        return False
    ext = Path(path).suffix.lower()
    return ext in SCRIPT_EXTS


def _stack_workload(command: str, stack: str) -> bool:
    if not stack or stack not in command:
        return False
    if _is_scanner(command):
        return False
    toks = _tokens(command)
    if not toks:
        return False
    argv0 = Path(toks[0]).name.lower()
    if argv0.startswith("python") or argv0 in INTERPRETERS:
        return any(stack in t and _looks_like_script(t) for t in toks[1:])
    return any(t.startswith(stack) and _looks_like_script(t) for t in toks)


def _keyword_hit(command: str) -> str | None:
    if _is_scanner(command):
        return None
    low = command.lower()
    toks = _tokens(command)
    blob = [low, *toks]
    for kw in KEYWORDS:
        for item in blob:
            if kw in item.lower():
                return kw
    # god_watch.py filename (underscore form of god-watch)
    if "god_watch" in low:
        return "god-watch"
    return None


def _demo_hit(command: str) -> tuple[str, str] | None:
    for script, spec in DEMO_SCRIPTS.items():
        if script in command:
            return spec
    return None


def _cli_basename(name: str) -> str | None:
    """Return grok/gemini if *name* is exactly that CLI (or gemini.js)."""
    if not name:
        return None
    low = name.lower()
    if low in CLI_BINS:
        return low
    stem = Path(low).stem
    suffix = Path(low).suffix.lower()
    if stem in CLI_BINS and suffix in {".js", ".mjs", ".cjs"}:
        return stem
    return None


# argv0 is bare `grok`/`gemini`, or a path whose final segment is exactly that
# (optional .js for node). Never matches ngrok/progrok or "Grok Bot.app"
# (spaces in Mac GUI paths would otherwise shlex-split into a false `Grok`).
_CLI_ARGV0_RE = re.compile(
    r"(?i)^(?P<bin>(?:[^\s]*/)?(?:grok|gemini|agy|antigravity)(?:\.js|\.mjs|\.cjs)?)(?:\s|$)"
)
_CLI_NODE_RE = re.compile(
    r"(?i)^(?:node|nodejs|python[\w.]*)\s+"
    r"(?P<bin>(?:[^\s]*/)?(?:grok|gemini|agy|antigravity)(?:\.js|\.mjs|\.cjs)?)(?:\s|$)"
)


def _cli_hit(command: str) -> str | None:
    """Return `grok` / `gemini` when argv0 (or node script) is that exact CLI."""
    if _is_scanner(command):
        return None
    # GUI bundles / denylist first — do not tokenize spaced .app paths.
    if is_denied_cmdline(command):
        return None
    low = command.strip()
    if not low:
        return None
    if ".app/" in low.lower() or low.lower().endswith(".app"):
        return None
    m = _CLI_ARGV0_RE.match(low) or _CLI_NODE_RE.match(low)
    if not m:
        return None
    return _cli_basename(Path(m.group("bin")).name)


def classify_proc(proc: Proc, *, stack: str, include_demo: bool) -> bool:
    """Return True if this process is a fleet candidate (incl. protected sessions)."""
    if is_denied_cmdline(proc.command):
        return False
    if _is_scanner(proc.command):
        return False
    # Show god/claude TUI sessions as read-only protected workers
    if is_god_session_wrapper(proc.command):
        return True
    if _cli_hit(proc.command):
        return True
    if include_demo and _demo_hit(proc.command):
        return True
    if _keyword_hit(proc.command):
        return True
    if _stack_workload(proc.command, stack):
        return True
    return False


def slug_for(proc: Proc, *, stack: str) -> str:
    if is_god_session_wrapper(proc.command):
        # Distinguish launcher vs Claude CLI session
        from pathlib import Path as _P
        for t in _tokens(proc.command):
            if not t or t.startswith("-"):
                continue
            name = _P(t).name.lower()
            if name == "claude":
                return "claude-session"
            if name == "god":
                return "god-session"
        return "god-session"
    cli = _cli_hit(proc.command)
    if cli:
        return CLI_SLUGS[cli]
    demo = _demo_hit(proc.command)
    if demo:
        return demo[0]
    kw = _keyword_hit(proc.command)
    if kw:
        return kw.replace("_", "-")
    toks = _tokens(proc.command)
    for t in toks:
        if stack and t.startswith(stack):
            name = Path(t.rstrip("/")).name
            if name and not name.startswith("-"):
                return _norm(name.split(".")[0])
    argv0 = Path(toks[0]).name if toks else "proc"
    return _norm(Path(argv0).stem or "proc")


def human_name(slug: str, command: str, pid: int | None = None) -> str:
    demo = _demo_hit(command)
    if demo:
        return demo[1]
    base = SLUG_NAMES.get(slug, slug.replace("-", " ").title())
    m = re.search(r"--profile\s+(\S+)", command)
    if m:
        base = f"{base} ({m.group(1)})"
    if pid is not None and slug in {
        "mcp-hands",
        "god-rt",
        "god-watch",
        "csuper",
        "campaign-harness",
        "grok-cli",
        "gemini-cli",
        "agy-cli",
        "god-session",
        "claude-session",
    }:
        base = f"{base} · {pid}"
    return base


def select_workers(
    procs: Iterable[Proc],
    *,
    deny_pids: set[int],
    include_demo: bool,
    stack: str,
) -> list[Proc]:
    """Filter, drop protected PIDs, and keep the child when parent+child both match."""
    kept: list[Proc] = []
    for proc in procs:
        if proc.pid in deny_pids:
            continue
        if proc.ppid in deny_pids and is_denied_cmdline(proc.command):
            continue
        if not classify_proc(proc, stack=stack, include_demo=include_demo):
            continue
        kept.append(proc)

    kept_pids = {p.pid for p in kept}
    # Prefer the deepest workload (python -m mcp_hands.server over `uv run`)
    # but KEEP god/claude session wrappers even when they parent another listed proc.
    parents = {p.ppid for p in kept if p.ppid in kept_pids}
    leaves = [
        p
        for p in kept
        if p.pid not in parents or is_god_session_wrapper(p.command)
    ]
    return leaves or kept


def assign_ids(procs: list[Proc], *, stack: str) -> list[tuple[str, Proc]]:
    """Stable slug + pid suffix (unique per live process)."""
    out: list[tuple[str, Proc]] = []
    used: set[str] = set()
    for proc in procs:
        slug = slug_for(proc, stack=stack)
        wid = f"{slug}-{proc.pid}"
        if wid in used:
            wid = f"{slug}-{proc.pid}-{proc.ppid}"
        used.add(wid)
        out.append((wid, proc))
    return out


def read_processes(user: str = DEFAULT_USER) -> list[Proc]:
    try:
        out = subprocess.check_output(
            ["ps", "-u", user, "-axo", "pid=,ppid=,tty=,state=,%cpu=,user=,command="],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    procs: list[Proc] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        # pid ppid tty state %cpu user command
        parts = line.split(None, 6)
        cpu = 0.0
        if len(parts) >= 7:
            pid_s, ppid_s, tty_s, state, cpu_s, user_s, command = parts
            try:
                cpu = float(cpu_s)
            except ValueError:
                cpu = 0.0
        else:
            parts6 = line.split(None, 5)
            if len(parts6) < 6:
                parts5 = line.split(None, 4)
                if len(parts5) < 5:
                    continue
                pid_s, ppid_s, state, user_s, command = parts5
                tty_s = "?"
            else:
                pid_s, ppid_s, tty_s, state, user_s, command = parts6
        try:
            pid = int(pid_s)
            ppid = int(ppid_s)
        except ValueError:
            continue
        procs.append(
            Proc(
                pid=pid,
                ppid=ppid,
                state=state,
                command=command,
                user=user_s,
                tty=tty_s,
                cpu=cpu,
            )
        )
    return procs


def denied_pids_by_port(ports: Iterable[int] = PROTECTED_PORTS) -> set[int]:
    port_list = ",".join(f"{p}" for p in ports)
    pids: set[int] = set()
    try:
        out = subprocess.check_output(
            ["lsof", "-nP", f"-iTCP:{port_list}", "-sTCP:LISTEN", "-t"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return pids
    for line in out.split():
        try:
            pids.add(int(line))
        except ValueError:
            continue
    return pids


def proc_state(pid: int, user: str = DEFAULT_USER) -> str | None:
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "state="],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return out or None


def pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def load_targets(path: Path = TARGETS_PATH) -> dict[str, str]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items()}
    return {}


def save_targets(targets: dict[str, str], path: Path = TARGETS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(targets, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def lookup_target(targets: dict[str, str], worker_id: str, pid: int | None) -> str | None:
    if worker_id in targets:
        return targets[worker_id]
    if pid is not None and str(pid) in targets:
        return targets[str(pid)]
    return None


class GodModeAdapter:
    """Live local-process adapter for the God Mode stack (and leftover demo scripts)."""

    def __init__(
        self,
        stack: str | None = None,
        user: str | None = None,
        include_demo: bool = True,
        targets_path: Path | None = None,
        _procs: list[Proc] | None = None,
        _deny_pids: set[int] | None = None,
        _scanner: Callable[[], list[Proc]] | None = None,
        _runner: Callable | None = None,
    ) -> None:
        self.stack = (stack or os.environ.get("GOD_STACK") or DEFAULT_STACK).rstrip("/")
        self.user = user or os.environ.get("ATC_DISCOVER_USER") or DEFAULT_USER
        self.include_demo = include_demo
        self.targets_path = Path(targets_path) if targets_path else TARGETS_PATH
        self._lock = Lock()
        self._procs_override = _procs
        self._deny_override = _deny_pids
        self._scanner = _scanner
        self._runner = _runner
        self._note = (
            "Live God Mode adapter. Discovers user processes under GOD_STACK "
            "and named workloads (god-rt, campaign-harness, csuper, god-watch, mcp_hands), "
            "plus terminal Grok / Gemini / Antigravity (agy) CLI (source=cli; exact basename match). "
            "Enriches each worker with session_tty / session_app / session_id / session_hint "
            "(tty + parent Terminal.app/iTerm/Claude.app/god launcher + Claude --resume). "
            "god/claude TUI sessions are listed as protected (source=session); Claude/Cursor/Grok Bot GUI stay denylisted. "
            "Redirect on God RT runs allowlisted quiet god-rt verbs (campaign_status/brief/list/ready/next/probe)."
        )
        self._last_steer: dict | None = None

    def _snapshot_procs(self) -> list[Proc]:
        if self._procs_override is not None:
            return list(self._procs_override)
        if self._scanner is not None:
            return list(self._scanner())
        return read_processes(self.user)

    def _deny_pids(self) -> set[int]:
        if self._deny_override is not None:
            return set(self._deny_override)
        return denied_pids_by_port()

    def _listed(self) -> list[tuple[str, Proc]]:
        selected = select_workers(
            self._snapshot_procs(),
            deny_pids=self._deny_pids(),
            include_demo=self.include_demo,
            stack=self.stack,
        )
        return assign_ids(selected, stack=self.stack)

    def _to_worker(
        self,
        worker_id: str,
        proc: Proc,
        targets: dict[str, str] | None = None,
        all_procs: list[Proc] | None = None,
        cwd_map: dict[int, str] | None = None,
        by_pid: dict[int, Proc] | None = None,
    ) -> Worker:
        from backend.session_insight import enrich_session

        targets = targets if targets is not None else load_targets(self.targets_path)
        status = WorkerStatus.PAUSED if proc.stopped else WorkerStatus.RUNNING
        target = lookup_target(targets, worker_id, proc.pid)
        cmd = proc.command
        cmd_short = cmd if len(cmd) <= 80 else cmd[:77] + "..."
        detail_cmd = cmd if len(cmd) <= 160 else cmd[:157] + "..."
        state_letter = (proc.state or "?")[:1]
        detail = f"[{state_letter}] {detail_cmd}"
        session_tree = False
        if all_procs is not None:
            session_tree = ancestor_is_protected_session(proc, all_procs)
        slug = slug_for(proc, stack=self.stack)
        protected = is_god_session_wrapper(proc.command)
        if protected:
            source = "session"
        elif slug in {"grok-cli", "gemini-cli", "agy-cli"}:
            source = "cli"
        else:
            source = "god"
        bp = by_pid
        if bp is None and all_procs is not None:
            bp = {p.pid: p for p in all_procs}
        sess = enrich_session(
            pid=proc.pid,
            ppid=proc.ppid,
            tty=proc.tty,
            command=proc.command,
            by_pid=bp or {},
            cwd_map=cwd_map,
            role=slug,
        )
        session_id = sess.session_id
        # God launcher: inherit Claude --resume from child when possible
        if session_id is None and protected and slug == "god-session":
            from backend.session_depth import child_or_self_session_id
            from backend.session_insight import format_session_hint, parse_claude_resume

            session_id = child_or_self_session_id(
                proc.pid, proc.command, bp or {}, parse_resume=parse_claude_resume
            )
            if session_id:
                hint = format_session_hint(
                    sess.tty, sess.app, session_id, sess.cwd, role=slug
                )
            else:
                hint = sess.hint
        else:
            hint = sess.hint

        # Idle/stale for session + CLI workers (CPU quiet / sleeping)
        from backend.session_depth import (
            compute_activity_status,
            find_claude_jsonl,
            god_last_answer_path,
        )

        preview = None
        activity = None
        if source in {"session", "cli"}:
            mtime = None
            if session_id:
                jp = find_claude_jsonl(session_id)
                if jp is not None:
                    try:
                        mtime = jp.stat().st_mtime
                    except OSError:
                        mtime = None
                if mtime is None:
                    gp = god_last_answer_path(session_id, stack=self.stack)
                    if gp is not None:
                        try:
                            mtime = gp.stat().st_mtime
                        except OSError:
                            mtime = None
            activity = compute_activity_status(
                state=proc.state,
                cpu=getattr(proc, "cpu", 0.0),
                source=source,
                transcript_mtime=mtime,
            )
            if activity == "paused":
                status = WorkerStatus.PAUSED
            elif activity == "idle":
                status = WorkerStatus.IDLE
            elif activity == "stale":
                status = WorkerStatus.STALE
            else:
                status = WorkerStatus.RUNNING

        cpu_pct = float(getattr(proc, "cpu", 0.0) or 0.0)
        detail = f"[{state_letter} cpu={cpu_pct:.1f}] {detail_cmd}"

        return Worker(
            id=worker_id,
            name=human_name(slug, proc.command, proc.pid),
            status=status,
            pid=proc.pid,
            target=target,
            detail=detail,
            updated_at=_now(),
            source=source,
            cmdline_short=cmd_short,
            uptime_sec=None,
            session_tree=session_tree,
            session_tty=sess.tty,
            session_app=sess.app,
            session_id=session_id,
            session_cwd=sess.cwd,
            session_hint=hint,
            protected=protected,
            cpu_pct=cpu_pct,
            activity=activity or status.value,
            left_off_preview=preview,
        )

    def list_workers(self) -> list[Worker]:
        """Read-only discovery — never signals or changes process state."""
        from backend.session_insight import read_cwds_cheap

        procs = self._snapshot_procs()
        targets = load_targets(self.targets_path)
        selected = select_workers(
            procs,
            deny_pids=self._deny_pids(),
            include_demo=self.include_demo,
            stack=self.stack,
        )
        assigned = assign_ids(selected, stack=self.stack)
        by_pid = {p.pid: p for p in procs}
        cwd_map = read_cwds_cheap(p.pid for _, p in assigned)
        return [
            self._to_worker(
                wid, proc, targets, all_procs=procs, cwd_map=cwd_map, by_pid=by_pid
            )
            for wid, proc in assigned
        ]

    def _require(self, worker_id: str) -> str:
        key = _norm(worker_id)
        compact = _compact(worker_id)
        listed = self._listed()
        workers = [(wid, proc) for wid, proc in listed]

        def _matches(wid: str, proc: Proc) -> bool:
            name = human_name(slug_for(proc, stack=self.stack), proc.command, proc.pid)
            if _norm(wid) == key or wid == worker_id.strip():
                return True
            if _norm(name) == key or _compact(name) == compact:
                return True
            if wid.startswith(key + "-") or _norm(wid).startswith(key + "-"):
                return True
            if str(proc.pid) == worker_id.strip():
                return True
            return False

        hits = [(wid, proc) for wid, proc in workers if _matches(wid, proc)]
        # de-dupe by id
        uniq: dict[str, Proc] = {}
        for wid, proc in hits:
            uniq[wid] = proc
        if len(uniq) == 1:
            return next(iter(uniq))
        if len(uniq) > 1:
            ids = ", ".join(sorted(uniq))
            raise KeyError(f"ambiguous worker {worker_id!r}: {ids}")
        raise KeyError(f"unknown worker: {worker_id}")

    def _get(self, worker_id: str) -> tuple[str, Proc]:
        wid = self._require(worker_id)
        for id_, proc in self._listed():
            if id_ == wid:
                return id_, proc
        raise KeyError(f"unknown worker: {worker_id}")

    def _guard(self, proc: Proc, *, op: str = "control") -> None:
        # GUI / system denylist: never signal.
        if is_denied_cmdline(proc.command):
            raise PermissionError(
                f"refusing to {op} protected process pid={proc.pid}"
            )
        if proc.pid in self._deny_pids():
            raise PermissionError(
                f"refusing to {op} protected process pid={proc.pid}"
            )
        # god/claude sessions are listed as protected; registry requires an explicit
        # id/PID before calling us. Collateral kill/CONT still skips wrappers.

    def pause(self, worker_id: str) -> Worker:
        """SIGSTOP the listed PID only — never the process group or wrappers."""
        wid, proc = self._get(worker_id)
        self._guard(proc, op="pause")
        if proc.stopped:
            return self._to_worker(wid, proc)
        try:
            os.kill(proc.pid, signal.SIGSTOP)
        except ProcessLookupError as e:
            raise RuntimeError(f"{wid} is gone (pid={proc.pid})") from e
        except PermissionError as e:
            raise PermissionError(f"cannot pause {wid} (pid={proc.pid}): {e}") from e
        state = proc_state(proc.pid, self.user) or "T"
        refreshed = Proc(proc.pid, proc.ppid, state, proc.command, proc.user, proc.tty, getattr(proc, "cpu", 0.0))
        worker = self._to_worker(wid, refreshed)
        worker.status = WorkerStatus.PAUSED
        worker.detail = f"paused via SIGSTOP (was {proc.command[:80]})"
        worker.updated_at = _now()
        return worker

    def resume(self, worker_id: str) -> Worker:
        """SIGCONT the worker and every descendant (heal leftover T children)."""
        wid, proc = self._get(worker_id)
        self._guard(proc, op="resume")
        snap = self._snapshot_procs()
        if is_god_session_wrapper(proc.command):
            tree = [proc.pid]  # explicit session: never CONT into the whole TUI tree
        else:
            tree = cont_target_pids(proc.pid, snap)
        saw_root = False
        for pid in tree:
            try:
                os.kill(pid, signal.SIGCONT)
                if pid == proc.pid:
                    saw_root = True
            except ProcessLookupError as e:
                if pid == proc.pid:
                    raise RuntimeError(f"{wid} is gone (pid={proc.pid})") from e
            except PermissionError as e:
                if pid == proc.pid:
                    raise PermissionError(f"cannot resume {wid} (pid={proc.pid}): {e}") from e
        if not saw_root and not pid_exists(proc.pid):
            raise RuntimeError(f"{wid} is gone (pid={proc.pid})")
        time.sleep(0.05)
        state = proc_state(proc.pid, self.user) or "S"
        refreshed = Proc(proc.pid, proc.ppid, state, proc.command, proc.user, proc.tty, getattr(proc, "cpu", 0.0))
        worker = self._to_worker(wid, refreshed)
        worker.status = WorkerStatus.RUNNING
        worker.detail = "resumed via SIGCONT"
        worker.updated_at = _now()
        return worker

    def kill(self, worker_id: str) -> Worker:
        wid, proc = self._get(worker_id)
        self._guard(proc, op="kill")
        old_pid = proc.pid
        snap = self._snapshot_procs()
        targets = kill_target_pids(proc, snap)
        # SIGKILL works on stopped processes; TERM may sit pending until CONT.
        # Never walk up into god/claude — targets are leaf (+ optional mcp uv parent).
        for pid in targets:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.time() + 3.0
        while time.time() < deadline and any(pid_exists(p) for p in targets):
            time.sleep(0.1)
        for pid in targets:
            if pid_exists(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        time.sleep(0.05)
        slug = slug_for(proc, stack=self.stack)
        return Worker(
            id=wid,
            name=human_name(slug, proc.command, proc.pid),
            status=WorkerStatus.KILLED,
            pid=None,
            target=lookup_target(load_targets(self.targets_path), wid, old_pid),
            detail=f"killed (was pid={old_pid})",
            updated_at=_now(),
            source="cli" if slug in {"grok-cli", "gemini-cli", "agy-cli"} else "god",
        )

    def pop_last_steer(self) -> dict | None:
        """Consume last steer payload (for registry/agent audit)."""
        payload = self._last_steer
        self._last_steer = None
        return payload

    def redirect(self, worker_id: str, target: str) -> Worker:
        """Persist clearance label; for God RT also run allowlisted quiet god-rt verb."""
        wid, proc = self._get(worker_id)
        self._guard(proc)
        label = (target or "").strip()
        with self._lock:
            targets = load_targets(self.targets_path)
            targets[wid] = label
            targets[str(proc.pid)] = label
            save_targets(targets, self.targets_path)
        worker = self._to_worker(wid, proc, targets)
        worker.target = label
        worker.updated_at = _now()

        if is_god_rt_worker(wid, proc.command):
            steer = run_quiet_steer(
                label,
                stack=self.stack,
                runner=self._runner,
            )
            self._last_steer = steer.as_dict()
            if steer.verb is None and steer.ok and (steer.summary == "label-only" or not steer.error):
                # Non-verb target: label-only (same as other workers)
                if worker.status != WorkerStatus.KILLED:
                    worker.status = WorkerStatus.RUNNING if not proc.stopped else WorkerStatus.PAUSED
                    worker.detail = f"redirected → {label}"
                return worker
            if not steer.ok:
                # Still persist label, but surface honest error in detail
                worker.detail = f"steer failed → {label}: {steer.error}"
                # Raise so voice/text get a clear failure (label already saved)
                raise RuntimeError(steer.error or f"god-rt steer failed for {label!r}")
            worker.status = WorkerStatus.RUNNING if not proc.stopped else WorkerStatus.PAUSED
            worker.detail = f"steered → {steer.verb}: {steer.summary}"
            return worker

        if worker.status != WorkerStatus.KILLED:
            worker.status = WorkerStatus.RUNNING if not proc.stopped else WorkerStatus.PAUSED
            worker.detail = f"redirected → {label}"
        self._last_steer = None
        return worker

    def restart(self, worker_id: str) -> Worker:
        wid, proc = self._get(worker_id)
        self._guard(proc)
        if proc.stopped:
            return self.resume(wid)
        raise RuntimeError(
            f"{wid} is a discovered process (pid={proc.pid}); "
            "restart is only supported for demo-owned workers. "
            "Resume if paused, or kill and start the process yourself."
        )


    def inspect_worker(self, worker_id: str, lines: int = 20) -> dict:
        """Best-effort log / session left-off for discovered processes.

        Never fall back to a random stack `*.log` (that glued the same
        god-red campaign tail onto agy / god-session / claude-session).
        Only accept logs that clearly belong to this worker.

        For session/cli workers, prefer Claude jsonl / god-last-answer /
        agy-or-grok own transcripts — NOT campaign logs.
        """
        wid, proc = self._get(worker_id)
        # Need full proc table for child session_id / enrich
        all_procs = self._snapshot_procs()
        by_pid = {p.pid: p for p in all_procs}
        snap_worker = self._to_worker(wid, proc, all_procs=all_procs, by_pid=by_pid)
        snap = snap_worker.snapshot()
        slug = slug_for(proc, stack=self.stack)

        # Session / CLI: left-off transcript first (never campaign logs)
        if snap.get("source") in {"session", "cli"} or slug in {
            "god-session",
            "claude-session",
            "agy-cli",
            "grok-cli",
            "gemini-cli",
        }:
            from backend.session_depth import resolve_left_off

            left = resolve_left_off(
                source=snap.get("source"),
                slug=slug,
                session_id=snap.get("session_id"),
                pid=proc.pid,
                cwd=snap.get("session_cwd"),
                stack=self.stack,
                lines=max(1, min(int(lines or 20), 120)),
            )
            lines_out = list(left.lines)
            if not lines_out and left.note:
                lines_out = [left.note]
            summary = (
                f"{snap.get('name') or wid}: left off — "
                + (lines_out[-1] if lines_out else (left.note or "no transcript yet"))
            )
            if len(summary) > 200:
                summary = summary[:197] + "..."
            return {
                "worker_id": wid,
                "worker": snap,
                "source": snap.get("source") or "session",
                "log_path": left.source_path,
                "lines": lines_out,
                "line_count": len(lines_out),
                "target": snap.get("target"),
                "cmdline": snap.get("cmdline_short") or proc.command[:160],
                "state": proc.state,
                "cpu_pct": snap.get("cpu_pct"),
                "activity": snap.get("activity") or snap.get("status"),
                "session_id": snap.get("session_id"),
                "left_off": left.as_dict(),
                "summary": summary,
                "note": left.note or left.source_kind or "session transcript",
            }

        candidates: list[Path] = []
        # cmdline may point at a .log file — only keep if path mentions pid/slug/wid
        for tok in _tokens(proc.command):
            if tok.endswith(".log") or "/logs/" in tok:
                path = Path(tok)
                low = str(path).lower()
                if (
                    str(proc.pid) in low
                    or slug.lower() in low
                    or wid.lower() in low
                    or (snap.get("session_id") and str(snap.get("session_id")).lower()[:8] in low)
                ):
                    candidates.append(path)
        # stack logs — slug/pid only (NO bare *.log)
        stack_logs = Path(self.stack) / "logs"
        if stack_logs.is_dir():
            for pattern in (f"{slug}*.log", f"*{proc.pid}*.log", f"*{wid}*.log"):
                candidates.extend(sorted(stack_logs.glob(pattern), key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True)[:5])
        # ATC logs by slug / wid only
        atc_logs = ROOT / "logs"
        for name in (f"{slug}.log", f"{wid}.log"):
            candidates.append(atc_logs / name)

        seen: set[str] = set()
        chosen: Path | None = None
        for c in candidates:
            try:
                key = str(c.resolve())
            except OSError:
                key = str(c)
            if key in seen:
                continue
            seen.add(key)
            if c.is_file():
                chosen = c
                break

        if chosen is not None:
            tailed = tail_log_file(chosen, lines)
            last = tailed[-1].strip() if tailed else ""
            if last and len(last) > 120:
                last = last[:117] + "..."
            summary = (
                f"{snap.get('name') or wid}: last activity — {last} ({len(tailed)} lines tailed)."
                if tailed
                else f"{snap.get('name') or wid} log empty."
            )
            return {
                "worker_id": wid,
                "worker": snap,
                "source": snap.get("source") or "god",
                "log_path": str(chosen),
                "lines": tailed,
                "line_count": len(tailed),
                "target": snap.get("target"),
                "cmdline": snap.get("cmdline_short") or proc.command[:160],
                "state": proc.state,
                "summary": summary,
            }

        cmdline = snap.get("cmdline_short") or proc.command[:160]
        return {
            "worker_id": wid,
            "worker": snap,
            "source": snap.get("source") or "god",
            "log_path": None,
            "lines": [],
            "line_count": 0,
            "target": snap.get("target"),
            "cmdline": cmdline,
            "state": proc.state,
            "summary": f"{snap.get('name') or wid} is {snap.get('status')} (state {proc.state}); no log file.",
            "note": "no log file",
        }


    def steer_prompt(
        self,
        worker_id: str,
        prompt: str,
        *,
        method: str = "auto",
    ) -> dict:
        """Deliver an operator prompt to a live session/CLI (inbox + optional tty).

        Protected god/claude: allowed (steer-prompt only). Never signals.
        God RT quiet verbs still go through redirect(); this is free-text prompt.
        """
        from backend.session_depth import deliver_steer_prompt

        wid, proc = self._get(worker_id)
        # Read-only enrich for session_id / tty — no _guard SIG path
        all_procs = self._snapshot_procs()
        by_pid = {p.pid: p for p in all_procs}
        worker = self._to_worker(wid, proc, all_procs=all_procs, by_pid=by_pid)
        # Refuse GUI denylist / ATC itself, but allow protected sessions
        if is_denied_cmdline(proc.command):
            raise PermissionError(
                f"refusing to steer-prompt protected process pid={proc.pid}"
            )
        if proc.pid in self._deny_pids():
            raise PermissionError(
                f"refusing to steer-prompt protected process pid={proc.pid}"
            )
        delivery = deliver_steer_prompt(
            worker_id=wid,
            prompt=prompt,
            session_id=worker.session_id,
            tty=worker.session_tty or (None if proc.tty in {"?", "??"} else proc.tty),
            pid=proc.pid,
            source=worker.source,
            method=method,
        )
        self._last_steer = {
            "kind": "steer_prompt",
            **delivery.as_dict(),
        }
        return delivery.as_dict()

    def describe_interface(self) -> dict[str, Any]:
        workers = self.list_workers()
        return {
            "name": "GodModeAdapter",
            "status": "live",
            "worker_count": len(workers),
            "methods": ["list_workers", "pause", "resume", "kill", "redirect", "restart", "inspect_worker", "steer_prompt"],
            "stack": self.stack,
            "include_demo": self.include_demo,
            "notes": self._note,
            "workers": [w.snapshot() for w in workers],
        }
