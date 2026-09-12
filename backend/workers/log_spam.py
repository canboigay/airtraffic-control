#!/usr/bin/env python3
"""Demo worker: periodic log spam (simulates chatty agent)."""
import os
import signal
import sys
import time

_paused = False


def _handle_sigstop_style(signum, frame):  # noqa: ARG001
    global _paused
    if signum == signal.SIGUSR1:
        _paused = True
        print(f"[log-spam] PAUSED pid={os.getpid()}", flush=True)
    elif signum == signal.SIGUSR2:
        _paused = False
        print(f"[log-spam] RESUMED pid={os.getpid()}", flush=True)


def main() -> None:
    signal.signal(signal.SIGUSR1, _handle_sigstop_style)
    signal.signal(signal.SIGUSR2, _handle_sigstop_style)
    n = 0
    print(f"[log-spam] START pid={os.getpid()}", flush=True)
    while True:
        if not _paused:
            n += 1
            print(f"[log-spam] heartbeat #{n} pid={os.getpid()}", flush=True)
        time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
