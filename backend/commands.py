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
    lines: int | None = None
    # Memory / anaphora hints
    from_memory: bool = False
    memory_ref: str | None = None  # it|that|again|clarify
    scope: str | None = None  # demo|god|all for pause_all / resume_all


WORKER_ALIASES = {
    "log spam": "log-spam",
    "log-spam": "log-spam",
    "logspam": "log-spam",
    "log": "log-spam",
    "spam": "log-spam",
    "logger": "log-spam",
    "fake build": "fake-build",
    "fake-build": "fake-build",
    "fakebuild": "fake-build",
    "build": "fake-build",
    "builder": "fake-build",
    "fake research": "fake-research",
    "fake-research": "fake-research",
    "fakeresearch": "fake-research",
    "research": "fake-research",
    "researcher": "fake-research",
    "god rt": "god-rt",
    "god-rt": "god-rt",
    "godrt": "god-rt",
    "god mode": "god-rt",
    "godmode": "god-rt",
    "god": "god-rt",
    "god session": "god-session",
    "god-session": "god-session",
    "godsession": "god-session",
    "claude session": "claude-session",
    "claude-session": "claude-session",
    "claudesession": "claude-session",
    "my god session": "god-session",
    "my claude session": "claude-session",
    "mcp hands": "mcp-hands",
    "mcp-hands": "mcp-hands",
    "mcphands": "mcp-hands",
    "grok cli": "grok-cli",
    "grok-cli": "grok-cli",
    "grokcli": "grok-cli",
    "grok": "grok-cli",
    "gemini cli": "gemini-cli",
    "gemini-cli": "gemini-cli",
    "geminicli": "gemini-cli",
    "gemini": "gemini-cli",
    "agy cli": "agy-cli",
    "agy-cli": "agy-cli",
    "agycli": "agy-cli",
    "agy": "agy-cli",
    "antigravity": "agy-cli",
    "antigravity cli": "agy-cli",
}

FILLER = re.compile(
    r"\b(please|can you|could you|would you|hey|tower|airtraffic|"
    r"atc|go ahead and|just|now|the|a|an|my|our|agent|worker|process)\b"
)

# Pronoun / follow-up patterns
_IT = re.compile(r"\b(it|that|them|him|her|this one|that one)\b")
_AGAIN = re.compile(
    r"\b(do that again|do it again|again|same again|repeat that|repeat it|"
    r"one more time|same thing)\b"
)
_CLARIFY = re.compile(
    r"\b(what did you mean|what do you mean|what was that|say that again|"
    r"clarify|explain that|huh|come again)\b"
)


def _normalize(text: str) -> str:
    t = text.lower().strip()
    t = t.replace("-", " ")
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = FILLER.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _find_worker(text: str) -> str | None:
    for alias in sorted(WORKER_ALIASES.keys(), key=len, reverse=True):
        if alias in text:
            return WORKER_ALIASES[alias]
    return None


def parse_command(transcript: str) -> ParsedCommand | None:
    raw = transcript.strip()
    text = _normalize(raw)
    if not text:
        return None

    # Memory: clarify / "what did you mean"
    if _CLARIFY.search(text):
        return ParsedCommand(
            action="clarify",
            raw=raw,
            from_memory=True,
            memory_ref="clarify",
        )

    # Memory: do that again / repeat
    if _AGAIN.search(text):
        return ParsedCommand(
            action="repeat_last",
            raw=raw,
            from_memory=True,
            memory_ref="again",
        )

    # confirm kill — many spoken variants
    if re.search(r"\bconfirm\b", text) and re.search(r"\b(kill|killing|terminate)\b", text):
        return ParsedCommand(action="confirm_kill", raw=raw, confirm_text="confirm kill")
    if text in {"confirm", "yes confirm", "yes kill", "do it", "affirmative", "confirmed"}:
        return ParsedCommand(action="confirm_kill", raw=raw, confirm_text="confirm kill")

    # pause all god / hold god fleet — explicit God workload fleet pause
    if (
        re.search(r"\bpause\s+all\s+god\b", text)
        or re.search(r"\bpause\s+god\s+(all|fleet)\b", text)
        or re.search(r"\bhold\s+(the\s+)?god\s+fleet\b", text)
        or text in {"pause all god", "pause god all", "pause god fleet", "hold god fleet"}
    ):
        return ParsedCommand(action="pause_all", raw=raw, scope="god")

    # pause all / hold the fleet — demo workers only
    if re.search(r"\bpause\s+all\b", text) or re.search(r"\bhold\s+(the\s+)?fleet\b", text):
        return ParsedCommand(action="pause_all", raw=raw, scope="demo")
    if text in {"pause all", "hold fleet", "hold the fleet"}:
        return ParsedCommand(action="pause_all", raw=raw, scope="demo")

    # resume all god
    if (
        re.search(r"\bresume\s+all\s+god\b", text)
        or re.search(r"\bresume\s+god\s+(all|fleet)\b", text)
        or re.search(r"\bwake\s+(all\s+)?god\b", text)
        or text in {"resume all god", "resume god all", "wake god"}
    ):
        return ParsedCommand(action="resume_all", raw=raw, scope="god")

    # resume all / wake everyone — demo workers only
    if (
        re.search(r"\bresume\s+all\b", text)
        or re.search(r"\bwake\s+(everyone|everybody|all)\b", text)
        or text in {"resume all", "wake everyone", "wake everybody"}
    ):
        return ParsedCommand(action="resume_all", raw=raw, scope="demo")

    # where / which terminal / what session is X
    # e.g. "which terminal is mcp-hands in?", "what session is god-rt?", "where is fake-build running?"
    if (
        re.search(r"\bwhich\s+terminal\b", text)
        or re.search(r"\bwhat\s+session\b", text)
        or re.search(r"\bwhere(?:\s+s|\s+is)\b", text)  # where's → where s after normalize
        or re.search(r"\bwhere\s+is\b", text)
        or re.search(r"\bwhich\s+session\b", text)
        or re.search(r"\bwhat\s+terminal\b", text)
        or re.search(r"\bmy\s+(god|claude)\s+session\b", text)
        or (text.startswith("where ") and _find_worker(text))
    ):
        wid = _find_worker(text)
        if not wid and re.search(r"\bmy\s+god\s+session\b", text):
            wid = "god-session"
        if not wid and re.search(r"\bmy\s+claude\s+session\b", text):
            wid = "claude-session"
        if wid:
            return ParsedCommand(action="session", worker_id=wid, raw=raw)
        if _IT.search(text):
            return ParsedCommand(
                action="session",
                worker_id=None,
                raw=raw,
                from_memory=True,
                memory_ref="it",
            )

    # inspect / tail / what's X doing / status of X logs
    inspect_hit = False
    wid = None
    if re.search(r"\b(inspect|tail|peek)\b", text):
        inspect_hit = True
        wid = _find_worker(text)
    elif re.search(r"\bwhat(?:s| is)?\b.+\bdoing\b", text) or re.search(
        r"\bwhat\s+is\b.+\bdoing\b", text
    ):
        inspect_hit = True
        wid = _find_worker(text)
    elif re.search(r"\bstatus\s+of\b.+\blogs\b", text):
        inspect_hit = True
        wid = _find_worker(text)
    elif re.search(r"\b(research|build|spam)\s+logs\b", text) or re.search(
        r"\blogs\s+of\b", text
    ):
        inspect_hit = True
        wid = _find_worker(text)
    if inspect_hit and wid:
        return ParsedCommand(action="inspect", worker_id=wid, raw=raw, lines=20)
    if inspect_hit and _IT.search(text):
        return ParsedCommand(
            action="inspect",
            worker_id=None,
            raw=raw,
            lines=20,
            from_memory=True,
            memory_ref="it",
        )

    # ask/tell god-rt for quiet verb (before bare "status" matching)
    m_ask = re.search(r"\b(?:ask|tell|have)\b(.+?)\b(?:for|to)\b(.+)$", text)
    if m_ask:
        left, right = m_ask.group(1).strip(), m_ask.group(2).strip()
        wid = _find_worker(left)
        if wid and right and ("god" in left or wid == "god-rt"):
            return ParsedCommand(action="redirect", worker_id=wid, target=right, raw=raw)

    # status (fleet) — after inspect so "status of research logs" doesn't steal
    if re.search(r"\b(status|report|sitrep|what.?s going on|how are we)\b", text) or text in {
        "status",
        "fleet",
        "update",
    }:
        if not re.search(
            r"\b(pause|resume|kill|redirect|steer|hold|inspect|tail|ask|tell)\b", text
        ):
            return ParsedCommand(action="status", raw=raw)

    # kill
    if re.search(r"\b(kill|terminate|destroy|shut down|shutdown)\b", text):
        wid = _find_worker(text)
        if wid:
            return ParsedCommand(action="kill", worker_id=wid, raw=raw)
        if _IT.search(text):
            return ParsedCommand(
                action="kill",
                worker_id=None,
                raw=raw,
                from_memory=True,
                memory_ref="it",
            )

    # pause / hold (single)
    if re.search(r"\b(pause|hold|freeze|suspend)\b", text):
        wid = _find_worker(text)
        if wid:
            return ParsedCommand(action="pause", worker_id=wid, raw=raw)
        if _IT.search(text) or text in {"pause", "hold", "freeze", "suspend"}:
            return ParsedCommand(
                action="pause",
                worker_id=None,
                raw=raw,
                from_memory=True,
                memory_ref="it",
            )

    # resume (single)
    if re.search(r"\b(resume|continue|unpause|restart|wake)\b", text):
        wid = _find_worker(text)
        if wid:
            return ParsedCommand(action="resume", worker_id=wid, raw=raw)
        if _IT.search(text) or text in {"resume", "continue", "unpause", "wake"}:
            return ParsedCommand(
                action="resume",
                worker_id=None,
                raw=raw,
                from_memory=True,
                memory_ref="it",
            )

    # steer / redirect … to …
    # "steer god-rt to brief", "redirect god rt to campaign status",
    # "ask god-rt for brief", "point god-rt at ready"
    m = re.search(
        r"\b(?:redirect|send|retarget|point|steer)\b(.+?)\b(?:to|towards|at)\b(.+)$",
        text,
    )
    if m:
        left, right = m.group(1).strip(), m.group(2).strip()
        wid = _find_worker(left) or _find_worker(text)
        if wid and right:
            return ParsedCommand(action="redirect", worker_id=wid, target=right, raw=raw)
        if (not wid) and right and _IT.search(left):
            return ParsedCommand(
                action="redirect",
                worker_id=None,
                target=right,
                raw=raw,
                from_memory=True,
                memory_ref="it",
            )

    return ParsedCommand(action="unknown", raw=raw)


def apply_memory(
    cmd: ParsedCommand,
    *,
    last_worker_id: str | None,
    last_action: str | None = None,
    last_target: str | None = None,
    last_turn: dict[str, Any] | None = None,
) -> ParsedCommand:
    """Fill missing worker / expand repeat_last / clarify from session memory."""
    if cmd.action == "clarify":
        if not last_turn:
            cmd.action = "unknown"
            return cmd
        # Rephrase as a status-ish chat; agent will speak from memory
        cmd.action = "clarify"
        cmd.worker_id = last_turn.get("worker_id") or last_worker_id
        cmd.target = last_turn.get("target") or last_target
        return cmd

    if cmd.action == "repeat_last":
        if not last_turn or not last_turn.get("action"):
            cmd.action = "unknown"
            return cmd
        act = last_turn["action"]
        # Map kill.arm → kill, etc.
        if act in {"kill.arm", "kill"}:
            act = "kill"
        if act == "redirect" or act == "steer":
            act = "redirect"
        cmd.action = act
        cmd.worker_id = last_turn.get("worker_id") or last_worker_id
        cmd.target = last_turn.get("target") or last_target
        cmd.from_memory = True
        cmd.memory_ref = "again"
        if act == "inspect":
            cmd.lines = 20
        return cmd

    if cmd.from_memory and cmd.memory_ref == "it" and not cmd.worker_id:
        if last_worker_id:
            cmd.worker_id = last_worker_id
        return cmd

    return cmd


def command_to_dict(cmd: ParsedCommand) -> dict[str, Any]:
    return {
        "action": cmd.action,
        "worker_id": cmd.worker_id,
        "target": cmd.target,
        "raw": cmd.raw,
        "confirm_text": cmd.confirm_text,
        "lines": cmd.lines,
        "from_memory": cmd.from_memory,
        "memory_ref": cmd.memory_ref,
        "scope": cmd.scope,
    }
