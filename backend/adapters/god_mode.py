"""Stub: Mac / God Mode adapter interface (for later integration).

This module documents how AeroVoice will talk to real agent fleets
(e.g. Signell workers, Cursor agents, local God Mode processes) without
implementing the production control plane yet.

Expected responsibilities for a future GodModeAdapter:
- Discover running agents via local process table / agent registry API
- Map voice worker names → agent IDs
- Pause / resume via SIGSTOP/SIGCONT or agent control API
- Kill with confirm gate (already enforced in Registry)
- Redirect: change agent task target / prompt / queue
- Emit before/after PID + status for audit receipts

Wire-up path:
  Registry(adapter=GodModeAdapter(...))  # replace DemoAdapter in main.py
"""

from __future__ import annotations

from typing import Any

from backend.registry import Worker, WorkerStatus


class GodModeAdapter:
    """Placeholder — raises NotImplementedError on all control methods."""

    def __init__(self, registry_url: str | None = None) -> None:
        self.registry_url = registry_url
        self._note = (
            "God Mode adapter stub. Use DemoAdapter for the hackathon MVP. "
            "Implement discovery against local agents when integrating."
        )

    def list_workers(self) -> list[Worker]:
        raise NotImplementedError(self._note)

    def pause(self, worker_id: str) -> Worker:
        raise NotImplementedError(self._note)

    def resume(self, worker_id: str) -> Worker:
        raise NotImplementedError(self._note)

    def kill(self, worker_id: str) -> Worker:
        raise NotImplementedError(self._note)

    def redirect(self, worker_id: str, target: str) -> Worker:
        raise NotImplementedError(self._note)

    def restart(self, worker_id: str) -> Worker:
        raise NotImplementedError(self._note)

    def describe_interface(self) -> dict[str, Any]:
        return {
            "name": "GodModeAdapter",
            "status": "stub",
            "methods": ["list_workers", "pause", "resume", "kill", "redirect", "restart"],
            "notes": self._note,
            "example_worker": Worker(
                id="example-agent",
                name="Example Agent",
                status=WorkerStatus.RUNNING,
                pid=None,
                detail="placeholder",
            ).snapshot(),
        }
