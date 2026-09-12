"""Worker registry with pause / resume / kill (confirm gate) / redirect."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Any, Protocol

ROOT = Path(__file__).resolve().parents[1]
LOGS_DIR = ROOT / "logs"


class WorkerStatus(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    KILLED = "killed"
    REDIRECTED = "redirected"
    STARTING = "starting"
    ERROR = "error"


@dataclass
class Worker:
    id: str
    name: str
    status: WorkerStatus
    pid: int | None = None
    target: str | None = None
    detail: str = ""
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    uptime_sec: int | None = None
    cmdline_short: str | None = None
    source: str | None = None  # "demo" | "god" | "cli"
    session_tree: bool = False  # descendant of a live god/claude wrapper
    session_tty: str | None = None
    session_app: str | None = None
    session_id: str | None = None  # Claude --resume UUID when present
    session_cwd: str | None = None
    session_hint: str | None = None  # short card label

    def snapshot(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d


class WorkerAdapter(Protocol):
    def list_workers(self) -> list[Worker]: ...
    def pause(self, worker_id: str) -> Worker: ...
    def resume(self, worker_id: str) -> Worker: ...
    def kill(self, worker_id: str) -> Worker: ...
    def redirect(self, worker_id: str, target: str) -> Worker: ...
    def restart(self, worker_id: str) -> Worker: ...


CONFIRM_PHRASE = "confirm kill"
FLEET_SCOPES = frozenset({"demo", "god", "all"})
BARE_SESSION_NAMES = frozenset({"god", "claude", "godmode"})


def _normalize_scope(scope: str | None) -> str:
    s = (scope or "demo").strip().lower()
    return s if s in FLEET_SCOPES else "demo"


def _worker_source(w: dict[str, Any]) -> str:
    return str(w.get("source") or "").strip().lower()


def _scope_includes(scope: str, source: str) -> bool:
    if scope == "all":
        return True
    if scope == "god":
        return source == "god"
    return source == "demo"


def _is_protected_worker(w: dict[str, Any]) -> bool:
    """GUI denylist + god/claude TUI wrappers. Do not use human name (false +)."""
    try:
        from backend.adapters.god_mode import is_denied_cmdline, is_god_session_wrapper
    except Exception:
        return False
    for blob in (w.get("cmdline_short"), w.get("detail")):
        if not blob:
            continue
        text = str(blob)
        if is_god_session_wrapper(text) or is_denied_cmdline(text):
            return True
    return False


def _kill_needs_explicit_id(requested: str, worker: dict[str, Any]) -> bool:
    """Bare 'god' / 'claude' must not arm kill — require a pid-qualified id."""
    raw = (requested or "").strip().lower()
    compact = "".join(ch for ch in raw if ch.isalnum())
    if compact in BARE_SESSION_NAMES:
        return True
    return False


def write_target_file(worker_id: str, target: str, logs_dir: Path | None = None) -> Path:
    """Persist redirect target so demo workers can pick it up."""
    base = logs_dir or LOGS_DIR
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{worker_id}.target"
    path.write_text((target or "").strip() + "\n", encoding="utf-8")
    return path


def read_target_file(worker_id: str, logs_dir: Path | None = None) -> str | None:
    base = logs_dir or LOGS_DIR
    path = base / f"{worker_id}.target"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def tail_log_file(path: Path, lines: int = 20) -> list[str]:
    """Return last N lines of a text log (best-effort)."""
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if not data:
        return []
    parts = data.splitlines()
    return parts[-max(1, lines) :]


class Registry:
    """In-memory registry wrapping an adapter + kill confirm gate."""

    def __init__(self, adapter: WorkerAdapter) -> None:
        self.adapter = adapter
        self._lock = Lock()
        self._pending_kills: dict[str, str] = {}  # worker_id -> pending token/ts

    def list_workers(self) -> list[dict[str, Any]]:
        return [w.snapshot() for w in self.adapter.list_workers()]

    def get(self, worker_id: str) -> dict[str, Any] | None:
        wid = self.resolve_id(worker_id) or worker_id
        for w in self.adapter.list_workers():
            if w.id == wid or w.name.lower() == worker_id.lower():
                return w.snapshot()
        return None

    def resolve_id(self, name_or_id: str) -> str | None:
        key = name_or_id.strip().lower().replace(" ", "-").replace("_", "-")
        compact_key = key.replace("-", "")
        workers = self.adapter.list_workers()
        exact: list[str] = []
        fuzzy: list[str] = []
        for w in workers:
            if w.id == key or w.id.lower() == key:
                exact.append(w.id)
                continue
            if w.name.lower().replace(" ", "-") == key:
                fuzzy.append(w.id)
                continue
            compact = w.name.lower().replace(" ", "").replace("-", "")
            if compact == compact_key:
                fuzzy.append(w.id)
                continue
            if w.id.lower().startswith(key + "-"):
                fuzzy.append(w.id)
                continue
            if w.pid is not None and str(w.pid) == name_or_id.strip():
                fuzzy.append(w.id)
        if exact:
            return exact[0]
        uniq = list(dict.fromkeys(fuzzy))
        if len(uniq) == 1:
            return uniq[0]
        return None

    def pause(self, worker_id: str) -> dict[str, Any]:
        before = self.get(worker_id)
        if before and _is_protected_worker(before):
            raise PermissionError(
                f"refusing to pause protected god/claude session {before.get('id')}"
            )
        worker = self.adapter.pause(worker_id)
        return {"before": before, "after": worker.snapshot()}

    def resume(self, worker_id: str) -> dict[str, Any]:
        before = self.get(worker_id)
        worker = self.adapter.resume(worker_id)
        return {"before": before, "after": worker.snapshot()}

    def pause_all(self, scope: str = "demo") -> dict[str, Any]:
        """Pause workers. Default scope is demo-only — never fleet-pause God/CLI sessions.

        Skips protected god/claude wrappers and anyone under those session trees.
        Explicit `pause <id>` still reaches a named god/cli workload (not a wrapper).
        Terminal Grok/Gemini CLI workers (source=cli) are excluded from demo/god fleet pause.
        """
        scope = _normalize_scope(scope)
        paused: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for w in self.list_workers():
            st = w.get("status")
            src = _worker_source(w)
            if _is_protected_worker(w):
                skipped.append({"id": w["id"], "status": st, "reason": "protected god/claude session"})
                continue
            if w.get("session_tree"):
                skipped.append({"id": w["id"], "status": st, "reason": "session tree (god/claude ancestor)"})
                continue
            if not _scope_includes(scope, src):
                skipped.append({
                    "id": w["id"],
                    "status": st,
                    "reason": f"{src or 'unknown'} worker excluded from {scope} fleet pause",
                })
                continue
            if st in {"paused", "killed"}:
                skipped.append({"id": w["id"], "status": st, "reason": f"already {st}"})
                continue
            try:
                result = self.pause(w["id"])
                paused.append(result["after"])
            except Exception as e:
                errors.append({"id": w["id"], "error": str(e)})
        return {
            "scope": scope,
            "paused_count": len(paused),
            "skipped_count": len(skipped),
            "error_count": len(errors),
            "paused": paused,
            "skipped": skipped,
            "errors": errors,
        }

    def resume_all(self, scope: str = "demo") -> dict[str, Any]:
        """Resume paused workers in scope. Default is demo-only.

        Adapter resume walks the descendant tree (SIGCONT children, not just the
        discovered PID). Killed demo workers restart via resume(); killed god
        workers are skipped (gone).
        """
        scope = _normalize_scope(scope)
        resumed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for w in self.list_workers():
            st = w.get("status")
            src = _worker_source(w)
            if not _scope_includes(scope, src):
                skipped.append({
                    "id": w["id"],
                    "status": st,
                    "reason": f"{src or 'unknown'} worker excluded from {scope} fleet resume",
                })
                continue
            if st == "running":
                skipped.append({"id": w["id"], "status": st, "reason": "already running"})
                continue
            if st == "killed" and src == "god":
                skipped.append({"id": w["id"], "status": st, "reason": "killed god worker"})
                continue
            try:
                result = self.resume(w["id"])
                resumed.append(result["after"])
            except Exception as e:
                errors.append({"id": w["id"], "error": str(e)})
        return {
            "scope": scope,
            "resumed_count": len(resumed),
            "skipped_count": len(skipped),
            "error_count": len(errors),
            "resumed": resumed,
            "skipped": skipped,
            "errors": errors,
        }

    def request_kill(self, worker_id: str) -> dict[str, Any]:
        """Arm kill; requires spoken confirm phrase before execute_kill."""
        wid = self.resolve_id(worker_id) or worker_id
        before = self.get(wid)
        if not before:
            raise KeyError(f"unknown worker: {worker_id}")
        if _is_protected_worker(before):
            raise PermissionError(
                f"refusing to kill protected god/claude session {wid}"
            )
        if _kill_needs_explicit_id(worker_id, before):
            raise PermissionError(
                f"kill of god/claude session requires an explicit id "
                f"(e.g. {before.get('id')}), not {worker_id!r}"
            )
        with self._lock:
            self._pending_kills[wid] = datetime.now(timezone.utc).isoformat()
        return {
            "armed": True,
            "worker_id": wid,
            "confirm_phrase": CONFIRM_PHRASE,
            "before": before,
            "message": f'Say "{CONFIRM_PHRASE}" to kill {before["name"]} (PID {before.get("pid")})',
        }

    def execute_kill(self, worker_id: str, confirm: str) -> dict[str, Any]:
        wid = self.resolve_id(worker_id) or worker_id
        normalized = " ".join(confirm.lower().strip().split())
        if normalized != CONFIRM_PHRASE:
            raise PermissionError(
                f'Kill requires confirm phrase "{CONFIRM_PHRASE}", got "{confirm}"'
            )
        with self._lock:
            if wid not in self._pending_kills:
                raise PermissionError(
                    "Kill not armed. Say kill <worker> first, then confirm kill."
                )
            del self._pending_kills[wid]
        before = self.get(wid)
        if before and _is_protected_worker(before):
            raise PermissionError(
                f"refusing to kill protected god/claude session {wid}"
            )
        worker = self.adapter.kill(wid)
        return {"before": before, "after": worker.snapshot(), "confirmed": True}

    def redirect(self, worker_id: str, target: str) -> dict[str, Any]:
        before = self.get(worker_id)
        worker = self.adapter.redirect(worker_id, target)
        out: dict[str, Any] = {"before": before, "after": worker.snapshot()}
        pop = getattr(self.adapter, "pop_last_steer", None)
        if callable(pop):
            steer = pop()
            if steer:
                out["steer"] = steer
        return out

    def restart(self, worker_id: str) -> dict[str, Any]:
        before = self.get(worker_id)
        worker = self.adapter.restart(worker_id)
        return {"before": before, "after": worker.snapshot()}

    def inspect_worker(self, worker_id: str, lines: int = 20) -> dict[str, Any]:
        """Tail logs for a worker (demo log file or god-mode best-effort)."""
        lines = max(1, min(int(lines or 20), 200))
        wid = self.resolve_id(worker_id) or worker_id
        snap = self.get(wid)
        if not snap:
            raise KeyError(f"unknown worker: {worker_id}")

        # Prefer adapter-specific inspect when available
        inspect_fn = getattr(self.adapter, "inspect_worker", None)
        if callable(inspect_fn):
            try:
                payload = inspect_fn(wid, lines=lines)
                if isinstance(payload, dict):
                    payload.setdefault("worker", snap)
                    payload.setdefault("worker_id", wid)
                    return payload
            except Exception:
                pass

        # Demo / default: logs/{id}.log
        log_path = LOGS_DIR / f"{wid}.log"
        # Also try base slug for god ids like fake-build-12345
        if not log_path.exists():
            base = wid.rsplit("-", 1)[0] if wid.count("-") >= 1 else wid
            alt = LOGS_DIR / f"{base}.log"
            if alt.exists():
                log_path = alt
                wid_for_target = base
            else:
                wid_for_target = wid
        else:
            wid_for_target = wid

        if log_path.exists():
            tailed = tail_log_file(log_path, lines)
            target = snap.get("target") or read_target_file(wid_for_target)
            summary = _summarize_log_lines(tailed, snap.get("name") or wid)
            return {
                "worker_id": wid,
                "worker": snap,
                "source": snap.get("source") or "demo",
                "log_path": str(log_path),
                "lines": tailed,
                "line_count": len(tailed),
                "target": target,
                "summary": summary,
            }

        # No log file — return cmdline + state
        cmdline = snap.get("cmdline_short") or snap.get("detail") or ""
        return {
            "worker_id": wid,
            "worker": snap,
            "source": snap.get("source") or "god",
            "log_path": None,
            "lines": [],
            "line_count": 0,
            "target": snap.get("target"),
            "cmdline": cmdline,
            "state": snap.get("status"),
            "summary": f"{snap.get('name') or wid} is {snap.get('status')}; no log file.",
            "note": "no log file",
        }

    def status_summary(self) -> dict[str, Any]:
        workers = self.list_workers()
        return {
            "count": len(workers),
            "workers": workers,
            "by_status": {
                s.value: sum(1 for w in workers if w["status"] == s.value)
                for s in WorkerStatus
            },
        }


def _summarize_log_lines(lines: list[str], name: str) -> str:
    if not lines:
        return f"{name} has no recent log lines."
    last = lines[-1].strip()
    if len(last) > 120:
        last = last[:117] + "..."
    n = len(lines)
    return f"{name}: last activity — {last} ({n} lines tailed)."
