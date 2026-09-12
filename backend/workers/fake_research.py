#!/usr/bin/env python3
"""Demo worker: fake research agent scanning sources."""
import os
import signal
import sys
import time
from pathlib import Path

_paused = False
_target = ""
_worker_id = os.environ.get("ATC_WORKER_ID", "fake-research")
_logs_dir = Path(os.environ.get("ATC_LOGS_DIR") or (Path(__file__).resolve().parents[2] / "logs"))

SOURCES = ["arxiv", "docs", "github", "blog", "rfc"]


def _read_target() -> str:
    global _target
    path = _logs_dir / f"{_worker_id}.target"
    try:
        text = path.read_text(encoding="utf-8").strip()
        if text:
            _target = text
    except OSError:
        env_t = os.environ.get("ATC_TARGET", "").strip()
        if env_t:
            _target = env_t
    return _target


def _handle(signum, frame):  # noqa: ARG001
    global _paused
    if signum == signal.SIGUSR1:
        _paused = True
        print(f"[fake-research] PAUSED pid={os.getpid()}", flush=True)
    elif signum == signal.SIGUSR2:
        _paused = False
        print(f"[fake-research] RESUMED pid={os.getpid()}", flush=True)


def main() -> None:
    signal.signal(signal.SIGUSR1, _handle)
    signal.signal(signal.SIGUSR2, _handle)
    i = 0
    _read_target()
    print(f"[fake-research] START pid={os.getpid()} target={_target or 'default'}", flush=True)
    while True:
        _read_target()
        if not _paused:
            src = _target if _target else SOURCES[i % len(SOURCES)]
            print(f"[fake-research] scanning={src} target={_target or 'default'} pid={os.getpid()}", flush=True)
            i += 1
        time.sleep(2.5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
