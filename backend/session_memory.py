"""Short rolling tower dialogue memory for anaphora across turns.

Keeps last N user utterances + spoken replies + last action/worker/target so
"pause it", "do that again", "what did you mean by that" resolve against recent
context instead of a blank turn.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Turn:
    heard: str
    spoken_reply: str = ""
    action: str | None = None
    worker_id: str | None = None
    target: str | None = None
    ok: bool | None = None
    source: str = "voice"
    at: str = field(default_factory=_now)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class SessionMemory:
    """Process-local rolling history (shared by voice + text command paths)."""

    def __init__(self, maxlen: int = 12) -> None:
        self._lock = Lock()
        self._turns: deque[Turn] = deque(maxlen=max(2, int(maxlen)))
        # UI slide-out panel focus — preferred anaphora target while open
        self._focus_worker_id: str | None = None

    def clear(self) -> None:
        with self._lock:
            self._turns.clear()

    def set_focus(self, worker_id: str | None) -> None:
        """Set or clear the tower focus worker (session panel selection)."""
        wid = (worker_id or "").strip() or None
        with self._lock:
            self._focus_worker_id = wid

    def focus_worker_id(self) -> str | None:
        with self._lock:
            return self._focus_worker_id

    def clear_focus(self) -> None:
        with self._lock:
            self._focus_worker_id = None

    def record(
        self,
        heard: str,
        *,
        spoken_reply: str = "",
        action: str | None = None,
        worker_id: str | None = None,
        target: str | None = None,
        ok: bool | None = None,
        source: str = "voice",
    ) -> Turn:
        turn = Turn(
            heard=(heard or "").strip(),
            spoken_reply=(spoken_reply or "").strip(),
            action=action,
            worker_id=worker_id,
            target=target,
            ok=ok,
            source=source,
        )
        with self._lock:
            self._turns.append(turn)
        return turn

    def turns(self) -> list[Turn]:
        with self._lock:
            return list(self._turns)

    def last(self) -> Turn | None:
        with self._lock:
            return self._turns[-1] if self._turns else None

    def last_worker_id(self) -> str | None:
        """Prefer open panel focus, else most recent turn with a worker."""
        with self._lock:
            if self._focus_worker_id:
                return self._focus_worker_id
            for turn in reversed(self._turns):
                if turn.worker_id:
                    return turn.worker_id
        return None

    def last_action(self) -> str | None:
        with self._lock:
            for turn in reversed(self._turns):
                if turn.action and turn.action not in {"chat", "unknown", "status"}:
                    return turn.action
        return None

    def last_target(self) -> str | None:
        with self._lock:
            for turn in reversed(self._turns):
                if turn.target:
                    return turn.target
        return None

    def last_controllable(self) -> Turn | None:
        """Most recent turn that did a real fleet action (for 'do that again')."""
        skip = {"chat", "unknown", "status", "audit", None}
        with self._lock:
            for turn in reversed(self._turns):
                if turn.action and turn.action not in skip and turn.ok is not False:
                    return turn
        return None

    def prompt_block(self, *, limit: int = 8) -> str:
        """Compact recent dialogue for the LLM system prompt."""
        with self._lock:
            recent = list(self._turns)[-limit:]
        if not recent:
            return "Recent dialogue: (none yet this session)"
        lines = ["Recent dialogue (oldest→newest):"]
        for t in recent:
            bits = [f"user: {t.heard!r}"]
            if t.spoken_reply:
                bits.append(f"tower: {t.spoken_reply!r}")
            meta = []
            if t.action:
                meta.append(f"action={t.action}")
            if t.worker_id:
                meta.append(f"worker={t.worker_id}")
            if t.target:
                meta.append(f"target={t.target}")
            if meta:
                bits.append("(" + ", ".join(meta) + ")")
            lines.append("- " + " | ".join(bits))
        focus = self.focus_worker_id()
        lw = self.last_worker_id()
        la = self.last_action()
        if focus:
            lines.append(
                f"UI focus (slide-out panel open): worker={focus}. "
                "When the utterance is unambiguous about 'it'/that/this session "
                "(pause it, what's it doing, steer it to …), resolve to this focus worker."
            )
        if lw or la:
            lines.append(
                f"Anaphora defaults: last_worker={lw or '—'} last_action={la or '—'} "
                f"last_target={self.last_target() or '—'} focus={focus or '—'}."
            )
        lines.append(
            "Resolve pronouns (it/that/them/him) and 'again'/'same' against this history "
            "when the current utterance omits the worker or action. "
            "Prefer UI focus over dialogue history when the panel is open."
        )
        return "\n".join(lines)

    def snapshot(self) -> dict[str, Any]:
        # Compute under one lock (methods like last_worker_id also take the lock).
        with self._lock:
            turns = list(self._turns)
            focus = self._focus_worker_id
        last_w = focus
        if not last_w:
            for turn in reversed(turns):
                if turn.worker_id:
                    last_w = turn.worker_id
                    break
        last_a = None
        for turn in reversed(turns):
            if turn.action and turn.action not in {"chat", "unknown", "status"}:
                last_a = turn.action
                break
        last_t = None
        for turn in reversed(turns):
            if turn.target:
                last_t = turn.target
                break
        return {
            "count": len(turns),
            "focus_worker_id": focus,
            "last_worker_id": last_w,
            "last_action": last_a,
            "last_target": last_t,
            "turns": [t.as_dict() for t in turns],
        }


# Shared singleton used by FastAPI command handlers
tower_memory = SessionMemory(maxlen=12)
