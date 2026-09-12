"""Hybrid fleet: demo workers + discovered God Mode processes."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from backend.adapters.demo import DemoAdapter
from backend.adapters.god_mode import GodModeAdapter
from backend.registry import Worker

AdapterMode = Literal["local", "demo", "hybrid"]


def adapter_mode(raw: str | None = None) -> AdapterMode:
    value = (raw if raw is not None else os.environ.get("ATC_ADAPTER", "hybrid")).strip().lower()
    if value in {"local", "god", "god-mode", "god_mode"}:
        return "local"
    if value == "demo":
        return "demo"
    return "hybrid"


class CompositeAdapter:
    """Route control to the adapter that owns a worker id."""

    def __init__(self, *adapters: DemoAdapter | GodModeAdapter) -> None:
        if not adapters:
            raise ValueError("CompositeAdapter needs at least one adapter")
        self.adapters = list(adapters)

    def list_workers(self) -> list[Worker]:
        seen_ids: set[str] = set()
        seen_pids: set[int] = set()
        out: list[Worker] = []
        for adapter in self.adapters:
            for worker in adapter.list_workers():
                if worker.id in seen_ids:
                    continue
                if worker.pid is not None and worker.pid in seen_pids:
                    continue
                seen_ids.add(worker.id)
                if worker.pid is not None:
                    seen_pids.add(worker.pid)
                out.append(worker)
        return out

    def _route(self, worker_id: str) -> tuple[DemoAdapter | GodModeAdapter, str]:
        last_err: Exception | None = None
        for adapter in self.adapters:
            try:
                resolved = adapter._require(worker_id)
                return adapter, resolved
            except KeyError as e:
                last_err = e
        raise KeyError(str(last_err) if last_err else f"unknown worker: {worker_id}")

    def _require(self, worker_id: str) -> str:
        _, wid = self._route(worker_id)
        return wid

    def pause(self, worker_id: str) -> Worker:
        adapter, wid = self._route(worker_id)
        return adapter.pause(wid)

    def resume(self, worker_id: str) -> Worker:
        adapter, wid = self._route(worker_id)
        return adapter.resume(wid)

    def kill(self, worker_id: str) -> Worker:
        adapter, wid = self._route(worker_id)
        return adapter.kill(wid)

    def redirect(self, worker_id: str, target: str) -> Worker:
        adapter, wid = self._route(worker_id)
        worker = adapter.redirect(wid, target)
        # Stash steer from the owning adapter for registry.pop
        self._last_steer_adapter = adapter
        return worker

    def pop_last_steer(self) -> dict | None:
        adapter = getattr(self, "_last_steer_adapter", None)
        self._last_steer_adapter = None
        if adapter is None:
            return None
        fn = getattr(adapter, "pop_last_steer", None)
        if callable(fn):
            return fn()
        return None

    def restart(self, worker_id: str) -> Worker:
        adapter, wid = self._route(worker_id)
        return adapter.restart(wid)


    def steer_prompt(self, worker_id: str, prompt: str, *, method: str = "auto"):
        adapter, wid = self._route(worker_id)
        fn = getattr(adapter, "steer_prompt", None)
        if not callable(fn):
            raise RuntimeError(f"steer_prompt not supported for {worker_id}")
        result = fn(wid, prompt, method=method)
        self._last_steer_adapter = adapter
        return result

    def inspect_worker(self, worker_id: str, lines: int = 20) -> dict:
        adapter, wid = self._route(worker_id)
        fn = getattr(adapter, "inspect_worker", None)
        if callable(fn):
            return fn(wid, lines=lines)
        raise KeyError(f"inspect not supported for {worker_id}")



@dataclass
class Fleet:
    adapter: DemoAdapter | GodModeAdapter | CompositeAdapter
    demo: DemoAdapter | None
    god: GodModeAdapter | None
    mode: AdapterMode


def build_fleet(mode: str | None = None) -> Fleet:
    resolved = adapter_mode(mode)
    if resolved == "demo":
        demo = DemoAdapter()
        return Fleet(adapter=demo, demo=demo, god=None, mode=resolved)
    if resolved == "local":
        god = GodModeAdapter(include_demo=True)
        return Fleet(adapter=god, demo=None, god=god, mode=resolved)
    demo = DemoAdapter()
    god = GodModeAdapter(include_demo=False)
    return Fleet(adapter=CompositeAdapter(demo, god), demo=demo, god=god, mode=resolved)
