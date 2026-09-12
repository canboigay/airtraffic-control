#!/usr/bin/env python3
"""Demo worker: fake research agent scanning sources."""
import os
import signal
import sys
import time

_paused = False
SOURCES = ["arxiv", "docs", "github", "blog", "rfc"]


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
    print(f"[fake-research] START pid={os.getpid()}", flush=True)
    while True:
        if not _paused:
            src = SOURCES[i % len(SOURCES)]
            print(f"[fake-research] scanning={src} pid={os.getpid()}", flush=True)
            i += 1
        time.sleep(2.5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
