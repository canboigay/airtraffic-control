#!/usr/bin/env python3
"""Demo worker: fake CI build steps."""
import os
import signal
import sys
import time

_paused = False
STEPS = [
    "checkout",
    "install deps",
    "compile",
    "lint",
    "unit tests",
    "package",
    "upload artifact",
]


def _handle(signum, frame):  # noqa: ARG001
    global _paused
    if signum == signal.SIGUSR1:
        _paused = True
        print(f"[fake-build] PAUSED pid={os.getpid()}", flush=True)
    elif signum == signal.SIGUSR2:
        _paused = False
        print(f"[fake-build] RESUMED pid={os.getpid()}", flush=True)


def main() -> None:
    signal.signal(signal.SIGUSR1, _handle)
    signal.signal(signal.SIGUSR2, _handle)
    i = 0
    print(f"[fake-build] START pid={os.getpid()}", flush=True)
    while True:
        if not _paused:
            step = STEPS[i % len(STEPS)]
            print(f"[fake-build] step={step} pid={os.getpid()}", flush=True)
            i += 1
        time.sleep(3)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
