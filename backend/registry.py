"""Worker registry with pause / resume / kill (confirm gate) / redirect."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from threading import Lock
from typing import Any, Protocol


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


class Registry:
    """In-memory registry wrapping an adapter + kill confirm gate."""

    def __init__(self, adapter: WorkerAdapter) -> None:
        self.adapter = adapter
        self._lock = Lock()
        self._pending_kills: dict[str, str] = {}  # worker_id -> pending token/ts

    def list_workers(self) -> list[dict[str, Any]]:
        return [w.snapshot() for w in self.adapter.list_workers()]

    def get(self, worker_id: str) -> dict[str, Any] | None:
        for w in self.adapter.list_workers():
            if w.id == worker_id or w.name.lower() == worker_id.lower():
                return w.snapshot()
        return None

    def resolve_id(self, name_or_id: str) -> str | None:
        key = name_or_id.strip().lower().replace(" ", "-").replace("_", "-")
        for w in self.adapter.list_workers():
            if w.id == key or w.name.lower().replace(" ", "-") == key:
                return w.id
            # fuzzy: "log spam" / "logspam" / "log-spam"
            compact = w.name.lower().replace(" ", "").replace("-", "")
            if compact == key.replace("-", ""):
                return w.id
        return None

    def pause(self, worker_id: str) -> dict[str, Any]:
        before = self.get(worker_id)
        worker = self.adapter.pause(worker_id)
        return {"before": before, "after": worker.snapshot()}

    def resume(self, worker_id: str) -> dict[str, Any]:
        before = self.get(worker_id)
        worker = self.adapter.resume(worker_id)
        return {"before": before, "after": worker.snapshot()}

    def request_kill(self, worker_id: str) -> dict[str, Any]:
        """Arm kill; requires spoken confirm phrase before execute_kill."""
        wid = self.resolve_id(worker_id) or worker_id
        before = self.get(wid)
        if not before:
            raise KeyError(f"unknown worker: {worker_id}")
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
        worker = self.adapter.kill(wid)
        return {"before": before, "after": worker.snapshot(), "confirmed": True}

    def redirect(self, worker_id: str, target: str) -> dict[str, Any]:
        before = self.get(worker_id)
        worker = self.adapter.redirect(worker_id, target)
        return {"before": before, "after": worker.snapshot()}

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
