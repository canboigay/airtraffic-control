"""Local God Mode adapter — discover and control real user processes.

Discovers Simeon's God Mode stack workloads (and optional ATC demo workers)
and exposes pause (SIGSTOP) / resume (SIGCONT) / kill (TERM then KILL) /
redirect (label + real god-rt quiet steer for God RT) / restart
(resume-if-paused only for discovered PIDs).
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
    # Live god launcher + claude TUI sessions: listed never, signalled never
    if is_god_session_wrapper(command):
        return True
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


def classify_proc(proc: Proc, *, stack: str, include_demo: bool) -> bool:
    """Return True if this process is a controllable candidate (denylist not applied)."""
    if is_denied_cmdline(proc.command):
        return False
    if _is_scanner(proc.command):
        return False
    if include_demo and _demo_hit(proc.command):
        return True
    if _keyword_hit(proc.command):
        return True
    if _stack_workload(proc.command, stack):
        return True
    return False


def slug_for(proc: Proc, *, stack: str) -> str:
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
    if pid is not None and slug in {"mcp-hands", "god-rt", "god-watch", "csuper", "campaign-harness"}:
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
    parents = {p.ppid for p in kept if p.ppid in kept_pids}
    leaves = [p for p in kept if p.pid not in parents]
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
            ["ps", "-u", user, "-axo", "pid=,ppid=,state=,user=,command="],
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
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        pid_s, ppid_s, state, user_s, command = parts
        try:
            pid = int(pid_s)
            ppid = int(ppid_s)
        except ValueError:
            continue
        procs.append(Proc(pid=pid, ppid=ppid, state=state, command=command, user=user_s))
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
            "and named workloads (god-rt, campaign-harness, csuper, god-watch, mcp_hands). "
            "Session wrappers (`…/god`, `claude` CLI) and Claude/Cursor/Grok GUI are denylisted. "
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
    ) -> Worker:
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
        return Worker(
            id=worker_id,
            name=human_name(slug_for(proc, stack=self.stack), proc.command, proc.pid),
            status=status,
            pid=proc.pid,
            target=target,
            detail=detail,
            updated_at=_now(),
            source="god",
            cmdline_short=cmd_short,
            uptime_sec=None,
            session_tree=session_tree,
        )

    def list_workers(self) -> list[Worker]:
        """Read-only discovery — never signals or changes process state."""
        procs = self._snapshot_procs()
        targets = load_targets(self.targets_path)
        selected = select_workers(
            procs,
            deny_pids=self._deny_pids(),
            include_demo=self.include_demo,
            stack=self.stack,
        )
        return [
            self._to_worker(wid, proc, targets, all_procs=procs)
            for wid, proc in assign_ids(selected, stack=self.stack)
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
        # SIGCONT is used to unstick trees; never refuse resume on denylist.
        if op == "resume":
            return
        if proc.pid in self._deny_pids() or is_denied_cmdline(proc.command):
            raise PermissionError(
                f"refusing to {op} protected process pid={proc.pid}"
            )

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
        refreshed = Proc(proc.pid, proc.ppid, state, proc.command, proc.user)
        worker = self._to_worker(wid, refreshed)
        worker.status = WorkerStatus.PAUSED
        worker.detail = f"paused via SIGSTOP (was {proc.command[:80]})"
        worker.updated_at = _now()
        return worker

    def resume(self, worker_id: str) -> Worker:
        """SIGCONT the worker and every descendant (heal leftover T children)."""
        wid, proc = self._get(worker_id)
        self._guard(proc, op="resume")
        tree = [proc.pid, *descendant_pids(self._snapshot_procs(), proc.pid)]
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
        refreshed = Proc(proc.pid, proc.ppid, state, proc.command, proc.user)
        worker = self._to_worker(wid, refreshed)
        worker.status = WorkerStatus.RUNNING
        worker.detail = "resumed via SIGCONT"
        worker.updated_at = _now()
        return worker

    def kill(self, worker_id: str) -> Worker:
        wid, proc = self._get(worker_id)
        self._guard(proc, op="kill")
        old_pid = proc.pid
        # SIGKILL works on stopped processes; TERM may sit pending until CONT.
        try:
            os.kill(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.time() + 3.0
        while time.time() < deadline and pid_exists(proc.pid):
            time.sleep(0.1)
        if pid_exists(proc.pid):
            try:
                os.kill(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            time.sleep(0.05)
        return Worker(
            id=wid,
            name=human_name(slug_for(proc, stack=self.stack), proc.command, proc.pid),
            status=WorkerStatus.KILLED,
            pid=None,
            target=lookup_target(load_targets(self.targets_path), wid, old_pid),
            detail=f"killed (was pid={old_pid})",
            updated_at=_now(),
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
        """Best-effort log tail for discovered processes."""
        wid, proc = self._get(worker_id)
        snap = self._to_worker(wid, proc).snapshot()
        candidates: list[Path] = []
        # cmdline may point at a .log file
        for tok in _tokens(proc.command):
            if tok.endswith(".log") or "/logs/" in tok:
                candidates.append(Path(tok))
        # stack logs directory
        stack_logs = Path(self.stack) / "logs"
        if stack_logs.is_dir():
            slug = slug_for(proc, stack=self.stack)
            for pattern in (f"{slug}*.log", f"*{proc.pid}*.log", "*.log"):
                candidates.extend(sorted(stack_logs.glob(pattern))[:5])
        # ATC demo leftover logs by slug
        atc_logs = ROOT / "logs"
        slug = slug_for(proc, stack=self.stack)
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
                "source": "god",
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
            "source": "god",
            "log_path": None,
            "lines": [],
            "line_count": 0,
            "target": snap.get("target"),
            "cmdline": cmdline,
            "state": proc.state,
            "summary": f"{snap.get('name') or wid} is {snap.get('status')} (state {proc.state}); no log file.",
            "note": "no log file",
        }

    def describe_interface(self) -> dict[str, Any]:
        workers = self.list_workers()
        return {
            "name": "GodModeAdapter",
            "status": "live",
            "worker_count": len(workers),
            "methods": ["list_workers", "pause", "resume", "kill", "redirect", "restart", "inspect_worker"],
            "stack": self.stack,
            "include_demo": self.include_demo,
            "notes": self._note,
            "workers": [w.snapshot() for w in workers],
        }
