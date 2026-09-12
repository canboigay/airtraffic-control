"""Show god/claude sessions as protected read-only workers."""

from __future__ import annotations

from backend.adapters.god_mode import GodModeAdapter, Proc
from backend.commands import parse_command
from backend.registry import Registry, Worker, WorkerStatus

STACK = "/Users/simeong/local-claude-offline-stack"


def _proc(pid, cmd, ppid=1, tty="ttys000"):
    return Proc(pid=pid, ppid=ppid, state="S", command=cmd, user="simeong", tty=tty)


def test_list_shows_god_and_claude_sessions_protected(tmp_path):
    terminal = _proc(704, "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal", tty="??")
    zsh = _proc(835, "-zsh", ppid=704)
    god = _proc(900, f"zsh {STACK}/god", ppid=835)
    claude = _proc(
        901,
        "claude --resume 146e90ce-078b-4f95-9200-1a4d52322c0c",
        ppid=900,
    )
    mcp = _proc(911, "python -m mcp_hands.server --profile quiet", ppid=835)
    adapter = GodModeAdapter(
        stack=STACK,
        include_demo=False,
        _procs=[terminal, zsh, god, claude, mcp],
        _deny_pids=set(),
        targets_path=tmp_path / "t.json",
    )
    by_id = {w.id: w for w in adapter.list_workers()}
    assert "god-session-900" in by_id
    assert "claude-session-901" in by_id
    assert "mcp-hands-911" in by_id
    assert by_id["god-session-900"].protected is True
    assert by_id["claude-session-901"].protected is True
    assert by_id["claude-session-901"].source == "session"
    assert by_id["claude-session-901"].session_id == "146e90ce-078b-4f95-9200-1a4d52322c0c"
    assert by_id["claude-session-901"].session_tty == "ttys000"
    assert "resume" in (by_id["claude-session-901"].session_hint or "")
    assert by_id["mcp-hands-911"].protected is False


def test_voice_where_is_my_god_session():
    cmd = parse_command("where is my god session?")
    assert cmd and cmd.action == "session" and cmd.worker_id == "god-session"
    cmd = parse_command("which terminal is my claude session in")
    assert cmd and cmd.action == "session" and cmd.worker_id == "claude-session"


def test_registry_bare_refuses_explicit_allows():
    w = Worker(
        id="god-session-900",
        name="God Session · 900",
        status=WorkerStatus.RUNNING,
        pid=900,
        source="session",
        protected=True,
        cmdline_short=f"zsh {STACK}/god",
        session_tty="ttys000",
        session_hint="ttys000 · Terminal.app · god-session",
    )

    class A:
        def __init__(self):
            self._w = [w]
            self.paused = []

        def list_workers(self):
            return list(self._w)

        def pause(self, worker_id):
            self.paused.append(worker_id)
            w.status = WorkerStatus.PAUSED
            return w

        def resume(self, worker_id):
            raise NotImplementedError

        def kill(self, worker_id):
            raise NotImplementedError

        def redirect(self, worker_id, target):
            raise NotImplementedError

        def restart(self, worker_id):
            raise NotImplementedError

    reg = Registry(A())
    import pytest
    with pytest.raises(PermissionError):
        reg.pause("god")
    out = reg.pause("god-session-900")
    assert out["after"]["status"] == "paused"
