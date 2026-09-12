"""Parse spoken ATC commands into structured actions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass
class ParsedCommand:
    action: str
    worker_id: str | None = None
    target: str | None = None
    raw: str = ""
    confirm_text: str | None = None


# Spoken aliases → canonical worker ids
WORKER_ALIASES = {
    "log spam": "log-spam",
    "log-spam": "log-spam",
    "logspam": "log-spam",
    "spam": "log-spam",
    "fake build": "fake-build",
    "fake-build": "fake-build",
    "build": "fake-build",
    "fake research": "fake-research",
    "fake-research": "fake-research",
    "research": "fake-research",
}


def _normalize(text: str) -> str:
    t = text.lower().strip()
    t = re.sub(r"[^\w\s\-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _find_worker(text: str) -> str | None:
    # longest alias first
    for alias in sorted(WORKER_ALIASES.keys(), key=len, reverse=True):
        if alias in text:
            return WORKER_ALIASES[alias]
    return None


def parse_command(transcript: str) -> ParsedCommand | None:
    raw = transcript.strip()
    text = _normalize(raw)
    if not text:
        return None

    # confirm kill (global)
    if text in ("confirm kill", "confirm kill please") or text.startswith("confirm kill"):
        return ParsedCommand(action="confirm_kill", raw=raw, confirm_text="confirm kill")

    if text in ("status", "status report", "what's the status", "whats the status", "fleet status"):
        return ParsedCommand(action="status", raw=raw)

    # kill <worker>
    m = re.match(r"^(?:kill|terminate|stop permanently)\s+(.+)$", text)
    if m:
        wid = _find_worker(m.group(1)) or m.group(1).replace(" ", "-")
        return ParsedCommand(action="kill", worker_id=wid, raw=raw)

    # pause / resume / redirect
    m = re.match(r"^(?:pause|hold)\s+(.+)$", text)
    if m:
        wid = _find_worker(m.group(1)) or m.group(1).replace(" ", "-")
        return ParsedCommand(action="pause", worker_id=wid, raw=raw)

    m = re.match(r"^(?:resume|continue|unpause)\s+(.+)$", text)
    if m:
        wid = _find_worker(m.group(1)) or m.group(1).replace(" ", "-")
        return ParsedCommand(action="resume", worker_id=wid, raw=raw)

    m = re.match(r"^(?:redirect|send|retarget)\s+(.+?)\s+(?:to|towards)\s+(.+)$", text)
    if m:
        wid = _find_worker(m.group(1)) or m.group(1).replace(" ", "-")
        return ParsedCommand(action="redirect", worker_id=wid, target=m.group(2).strip(), raw=raw)

    # "redirect <worker> <target>" without "to"
    m = re.match(r"^redirect\s+(\S+(?:\s+\S+)?)\s+(.+)$", text)
    if m and " to " not in text:
        wid = _find_worker(m.group(1)) or m.group(1).replace(" ", "-")
        return ParsedCommand(action="redirect", worker_id=wid, target=m.group(2).strip(), raw=raw)

    return ParsedCommand(action="unknown", raw=raw)


def command_to_dict(cmd: ParsedCommand) -> dict[str, Any]:
    return {
        "action": cmd.action,
        "worker_id": cmd.worker_id,
        "target": cmd.target,
        "raw": cmd.raw,
        "confirm_text": cmd.confirm_text,
    }
