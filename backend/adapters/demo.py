"""Demo adapter: spawn and manage 3 real local worker processes."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from backend.registry import Worker, WorkerStatus, read_target_file, tail_log_file, write_target_file

ROOT = Path(__file__).resolve().parents[2]
WORKERS_DIR = ROOT / "backend" / "workers"
LOGS_DIR = ROOT / "logs"

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
        self._started_at: dict[str, float] = {}

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

    def _cmdline_short(self, worker_id: str) -> str:
        script = self._script_path(worker_id).name
        return f"python {script}"

    def _uptime(self, worker_id: str) -> int | None:
        started = self._started_at.get(worker_id)
        if started is None:
            return None
        return max(0, int(time.time() - started))

    def _enrich(self, worker: Worker, worker_id: str) -> Worker:
        worker.source = "demo"
        worker.cmdline_short = self._cmdline_short(worker_id)
        worker.uptime_sec = self._uptime(worker_id) if worker.status != WorkerStatus.KILLED else None
        worker.session_app = "ATC demo"
        worker.session_hint = "ATC demo"
        worker.session_tty = None
        worker.session_id = None
        # Keep detail useful for UI tooltip
        if not worker.detail or worker.detail.startswith("script="):
            bits = [worker.cmdline_short]
            if worker.uptime_sec is not None:
                bits.append(f"up {worker.uptime_sec}s")
            worker.detail = " · ".join(bits)
        return worker

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

        log_path = LOGS_DIR / f"{worker_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Restore target from file if present
        file_target = read_target_file(worker_id, LOGS_DIR)
        if file_target:
            self._targets[worker_id] = file_target
        env = os.environ.copy()
        env["ATC_WORKER_ID"] = worker_id
        env["ATC_LOGS_DIR"] = str(LOGS_DIR)
        if self._targets.get(worker_id):
            env["ATC_TARGET"] = self._targets[worker_id]
        log_f = open(log_path, "a", buffering=1)  # noqa: SIM115
        proc = subprocess.Popen(
            [sys.executable, str(script)],
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
            cwd=str(ROOT),
        )
        self._started_at[worker_id] = time.time()
        worker = Worker(
            id=worker_id,
            name=spec["name"],
            status=WorkerStatus.RUNNING,
            pid=proc.pid,
            target=self._targets.get(worker_id),
            detail=f"script={script.name}",
            updated_at=_now(),
            source="demo",
            cmdline_short=self._cmdline_short(worker_id),
            uptime_sec=0,
        )
        worker = self._enrich(worker, worker_id)
        with self._lock:
            self._procs[worker_id] = proc
            self._meta[worker_id] = worker
        return worker

    def _refresh(self, worker_id: str) -> Worker:
        with self._lock:
            worker = self._meta[worker_id]
            proc = self._procs.get(worker_id)
            if worker.status == WorkerStatus.KILLED:
                return self._enrich(worker, worker_id)
            if proc is None or proc.poll() is not None:
                worker.status = WorkerStatus.KILLED
                worker.pid = None
                worker.updated_at = _now()
                worker.detail = "process exited"
                worker.uptime_sec = None
            else:
                worker.uptime_sec = self._uptime(worker_id)
            return self._enrich(worker, worker_id)

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
        worker = self._enrich(worker, wid)
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
        worker = self._enrich(worker, wid)
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
        worker.uptime_sec = None
        worker = self._enrich(worker, wid)
        with self._lock:
            self._meta[wid] = worker
            self._procs.pop(wid, None)
        return worker

    def redirect(self, worker_id: str, target: str) -> Worker:
        wid = self._require(worker_id)
        self._targets[wid] = target
        write_target_file(wid, target, LOGS_DIR)
        worker = self._refresh(wid)
        worker.target = target
        worker.status = WorkerStatus.REDIRECTED if worker.status != WorkerStatus.KILLED else worker.status
        if worker.status != WorkerStatus.KILLED:
            # mark redirected then keep running semantics
            worker.status = WorkerStatus.RUNNING
            worker.detail = f"redirected → {target}"
        worker.updated_at = _now()
        worker = self._enrich(worker, wid)
        # Prefer redirect detail over enrich overwrite when redirected
        if worker.status != WorkerStatus.KILLED:
            worker.detail = f"redirected → {target} · {worker.cmdline_short or ''}".strip(" ·")
        with self._lock:
            self._meta[wid] = worker
        return worker

    def restart(self, worker_id: str) -> Worker:
        wid = self._require(worker_id)
        return self._spawn(wid)

    def inspect_worker(self, worker_id: str, lines: int = 20) -> dict:
        wid = self._require(worker_id)
        snap = self._refresh(wid).snapshot()
        log_path = LOGS_DIR / f"{wid}.log"
        tailed = tail_log_file(log_path, lines) if log_path.exists() else []
        target = snap.get("target") or read_target_file(wid, LOGS_DIR)
        last = tailed[-1].strip() if tailed else ""
        if last and len(last) > 120:
            last = last[:117] + "..."
        summary = (
            f"{snap.get('name') or wid}: last activity — {last} ({len(tailed)} lines tailed)."
            if tailed
            else f"{snap.get('name') or wid} has no recent log lines."
        )
        return {
            "worker_id": wid,
            "worker": snap,
            "source": "demo",
            "log_path": str(log_path) if log_path.exists() else None,
            "lines": tailed,
            "line_count": len(tailed),
            "target": target,
            "summary": summary,
        }
