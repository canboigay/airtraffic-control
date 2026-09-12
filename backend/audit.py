"""Timestamped audit log for prove-it receipts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any
from uuid import uuid4


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AuditEntry:
    id: str
    ts: str
    action: str
    worker_id: str | None
    detail: dict[str, Any] = field(default_factory=dict)
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    source: str = "voice"


class AuditLog:
    def __init__(self, max_entries: int = 500) -> None:
        self._entries: list[AuditEntry] = []
        self._lock = Lock()
        self._max = max_entries

    def record(
        self,
        action: str,
        *,
        worker_id: str | None = None,
        detail: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        source: str = "voice",
    ) -> AuditEntry:
        entry = AuditEntry(
            id=str(uuid4()),
            ts=_utc_now(),
            action=action,
            worker_id=worker_id,
            detail=detail or {},
            before=before,
            after=after,
            source=source,
        )
        with self._lock:
            self._entries.append(entry)
            if len(self._entries) > self._max:
                self._entries = self._entries[-self._max :]
        return entry

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            items = self._entries[-limit:]
            return [asdict(e) for e in reversed(items)]


audit_log = AuditLog()
