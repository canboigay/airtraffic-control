"""Demo adapter: spawn and manage 3 real local worker processes."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from backend.registry import Worker, WorkerStatus

ROOT = Path(__file__).resolve().parents[2]
WORKERS_DIR = ROOT / "backend" / "workers"

WORKER_SPECS = [
    {"id": "log-spam", "name": "Log Spam", "script": "log_spam.py"},
    {"id": "fake-build", "name": "Fake Build", "script": "fake_build.py"},
    {"id": "fake-research", "name": "Fake Research", "script": "fake_research.py"},
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DemoAdapter:
    def __init__(self) -> None:
        self._lock = Lock()
        self._procs: dict[str, subprocess.Popen] = {}
        self._meta: dict[str, Worker] = {}
        self._targets: dict[str, str] = {}

    def start_all(self) -> None:
        for spec in WORKER_SPECS:
            self._spawn(spec["id"])

    def shutdown_all(self) -> None:
        with self._lock:
            ids = list(self._procs.keys())
        for wid in ids:
            try:
                self.kill(wid)
            except Exception:
                pass

    def _script_path(self, worker_id: str) -> Path:
        spec = next(s for s in WORKER_SPECS if s["id"] == worker_id)
        return WORKERS_DIR / spec["script"]

    def _spec(self, worker_id: str) -> dict:
        return next(s for s in WORKER_SPECS if s["id"] == worker_id)

    def _spawn(self, worker_id: str) -> Worker:
        spec = self._spec(worker_id)
        script = self._script_path(worker_id)
        # Kill existing if any
        with self._lock:
            old = self._procs.get(worker_id)
            if old and old.poll() is None:
                try:
                    old.terminate()
                    old.wait(timeout=2)
                except Exception:
                    try:
                        old.kill()
                    except Exception:
                        pass

        log_path = ROOT / "logs" / f"{worker_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_f = open(log_path, "a", buffering=1)  # noqa: SIM115
        proc = subprocess.Popen(
            [sys.executable, str(script)],
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        worker = Worker(
            id=worker_id,
            name=spec["name"],
            status=WorkerStatus.RUNNING,
            pid=proc.pid,
            target=self._targets.get(worker_id),
            detail=f"script={script.name}",
            updated_at=_now(),
        )
        with self._lock:
            self._procs[worker_id] = proc
            self._meta[worker_id] = worker
        return worker

    def _refresh(self, worker_id: str) -> Worker:
        with self._lock:
            worker = self._meta[worker_id]
            proc = self._procs.get(worker_id)
            if worker.status == WorkerStatus.KILLED:
                return worker
            if proc is None or proc.poll() is not None:
                worker.status = WorkerStatus.KILLED
                worker.pid = None
                worker.updated_at = _now()
                worker.detail = "process exited"
            return worker

    def list_workers(self) -> list[Worker]:
        out = []
        for spec in WORKER_SPECS:
            wid = spec["id"]
            if wid not in self._meta:
                continue
            out.append(self._refresh(wid))
        return out

    def _require(self, worker_id: str) -> str:
        # accept id or fuzzy name
        key = worker_id.strip().lower().replace(" ", "-").replace("_", "-")
        for spec in WORKER_SPECS:
            if spec["id"] == key:
                return spec["id"]
            compact = spec["name"].lower().replace(" ", "").replace("-", "")
            if compact == key.replace("-", ""):
                return spec["id"]
        raise KeyError(f"unknown worker: {worker_id}")

    def pause(self, worker_id: str) -> Worker:
        wid = self._require(worker_id)
        worker = self._refresh(wid)
        if worker.status == WorkerStatus.KILLED:
            raise RuntimeError(f"{wid} is killed; resume/restart first")
        with self._lock:
            proc = self._procs.get(wid)
        if proc and proc.poll() is None:
            os.kill(proc.pid, signal.SIGUSR1)
        worker.status = WorkerStatus.PAUSED
        worker.updated_at = _now()
        worker.detail = "paused via SIGUSR1"
        with self._lock:
            self._meta[wid] = worker
        return worker

    def resume(self, worker_id: str) -> Worker:
        wid = self._require(worker_id)
        worker = self._refresh(wid)
        if worker.status == WorkerStatus.KILLED or worker.pid is None:
            # restart if dead
            return self.restart(wid)
        with self._lock:
            proc = self._procs.get(wid)
        if proc and proc.poll() is None:
            os.kill(proc.pid, signal.SIGUSR2)
        worker.status = WorkerStatus.RUNNING
        worker.updated_at = _now()
        worker.detail = "resumed via SIGUSR2"
        with self._lock:
            self._meta[wid] = worker
        return worker

    def kill(self, worker_id: str) -> Worker:
        wid = self._require(worker_id)
        with self._lock:
            proc = self._procs.get(wid)
            worker = self._meta[wid]
        old_pid = worker.pid
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        worker.status = WorkerStatus.KILLED
        worker.pid = None
        worker.updated_at = _now()
        worker.detail = f"killed (was pid={old_pid})"
        with self._lock:
            self._meta[wid] = worker
            self._procs.pop(wid, None)
        return worker

    def redirect(self, worker_id: str, target: str) -> Worker:
        wid = self._require(worker_id)
        self._targets[wid] = target
        worker = self._refresh(wid)
        worker.target = target
        worker.status = WorkerStatus.REDIRECTED if worker.status != WorkerStatus.KILLED else worker.status
        if worker.status != WorkerStatus.KILLED:
            # mark redirected then keep running semantics
            worker.status = WorkerStatus.RUNNING
            worker.detail = f"redirected → {target}"
        worker.updated_at = _now()
        with self._lock:
            self._meta[wid] = worker
        return worker

    def restart(self, worker_id: str) -> Worker:
        wid = self._require(worker_id)
        return self._spawn(wid)
