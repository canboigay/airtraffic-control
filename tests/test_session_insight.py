"""Session/terminal insight on discovered workers + voice/text queries."""

from __future__ import annotations

from backend.adapters.god_mode import GodModeAdapter, Proc, select_workers
from backend.commands import parse_command
from backend.agent import _fast_fleet_action, _spoken_from_tool
from backend.registry import Registry, Worker, WorkerStatus
from backend.session_insight import (
    classify_session_app,
    enrich_session,
    format_session_hint,
    normalize_tty,
    parse_claude_resume,
    spoken_session_answer,
)

STACK = "/Users/simeong/local-claude-offline-stack"


def _proc(pid: int, cmd: str, ppid: int = 1, state: str = "S", tty: str = "??") -> Proc:
    return Proc(pid=pid, ppid=ppid, state=state, command=cmd, user="simeong", tty=tty)


def test_normalize_tty():
    assert normalize_tty("ttys003") == "ttys003"
    assert normalize_tty("??") is None
    assert normalize_tty("?") is None
    assert normalize_tty("") is None


def test_parse_claude_resume():
    cmd = (
        "claude --model x --resume 146e90ce-078b-4f95-9200-1a4d52322c0c"
    )
    assert parse_claude_resume(cmd) == "146e90ce-078b-4f95-9200-1a4d52322c0c"
    assert parse_claude_resume("claude --resume=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee") == (
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    )
    assert parse_claude_resume("god-rt watch") is None


def test_classify_session_app():
    assert classify_session_app("/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal") == "Terminal.app"
    assert classify_session_app("/Applications/iTerm.app/Contents/MacOS/iTerm2") == "iTerm"
    assert classify_session_app("/Applications/Claude.app/Contents/MacOS/Claude") == "Claude.app"
    assert classify_session_app(f"zsh {STACK}/god") == "god launcher"
    assert classify_session_app(f"{STACK}/god-rt watch") is None  # not the wrapper


def test_enrich_walks_parents_for_app_and_resume():
    terminal = _proc(704, "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal", tty="??")
    login = _proc(833, "login -pf simeong", ppid=704, tty="ttys000")
    zsh = _proc(835, "-zsh", ppid=833, tty="ttys000")
    god = _proc(900, f"zsh {STACK}/god", ppid=835, tty="ttys000")
    claude = _proc(
        901,
        "claude --resume 146e90ce-078b-4f95-9200-1a4d52322c0c",
        ppid=900,
        tty="ttys000",
    )
    mcp = _proc(
        43615,
        f"python -m mcp_hands.server --profile quiet",
        ppid=835,
        tty="ttys000",
    )
    by_pid = {p.pid: p for p in (terminal, login, zsh, god, claude, mcp)}

    sess_mcp = enrich_session(
        pid=mcp.pid, ppid=mcp.ppid, tty=mcp.tty, command=mcp.command, by_pid=by_pid
    )
    assert sess_mcp.tty == "ttys000"
    assert sess_mcp.app == "Terminal.app"
    assert sess_mcp.session_id is None  # resume is on sibling, not ancestor
    assert "ttys000" in (sess_mcp.hint or "")

    sess_claude = enrich_session(
        pid=claude.pid, ppid=claude.ppid, tty=claude.tty, command=claude.command, by_pid=by_pid
    )
    assert sess_claude.session_id == "146e90ce-078b-4f95-9200-1a4d52322c0c"
    assert sess_claude.app in {"Terminal.app", "god launcher", "Claude CLI"}
    assert "resume" in (sess_claude.hint or "")


def test_format_session_hint():
    assert format_session_hint("ttys003", "Terminal.app", None) == "ttys003 · Terminal.app"
    assert "resume 146e90ce" in (format_session_hint("ttys000", "god launcher", "146e90ce-078b-4f95-9200-1a4d52322c0c") or "")


def test_list_workers_includes_session_fields(tmp_path):
    terminal = _proc(704, "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal")
    shell = _proc(835, "-zsh", ppid=704, tty="ttys003")
    mcp = _proc(
        50100,
        f"{STACK}/.venv/bin/python -m mcp_hands.server --profile quiet",
        ppid=835,
        tty="ttys003",
    )
    god_rt = _proc(50200, f"zsh {STACK}/god-rt watch", ppid=835, tty="ttys004")
    adapter = GodModeAdapter(
        stack=STACK,
        include_demo=False,
        _procs=[terminal, shell, mcp, god_rt],
        _deny_pids=set(),
        targets_path=tmp_path / "t.json",
    )
    # Avoid live lsof in unit test: monkeypatch via empty cwd by not needing it
    workers = adapter.list_workers()
    by_id = {w.id: w for w in workers}
    assert "mcp-hands-50100" in by_id
    w = by_id["mcp-hands-50100"]
    assert w.session_tty == "ttys003"
    assert w.session_app == "Terminal.app"
    assert w.session_hint and "ttys003" in w.session_hint
    snap = w.snapshot()
    assert snap["session_tty"] == "ttys003"
    assert snap["session_app"] == "Terminal.app"
    assert "session_hint" in snap

    gr = by_id["god-rt-50200"]
    assert gr.session_tty == "ttys004"


def test_voice_session_queries():
    for utter, wid in [
        ("which terminal is mcp-hands in?", "mcp-hands"),
        ("what session is god-rt in?", "god-rt"),
        ("where is fake-build running?", "fake-build"),
        ("where's mcp hands", "mcp-hands"),
        ("what terminal is grok cli on", "grok-cli"),
    ]:
        cmd = parse_command(utter)
        assert cmd is not None, utter
        assert cmd.action == "session", (utter, cmd)
        assert cmd.worker_id == wid, (utter, cmd)


def test_spoken_session_answer():
    ans = spoken_session_answer(
        {
            "name": "MCP Hands · 50100",
            "session_tty": "ttys000",
            "session_app": "Terminal.app",
            "session_hint": "ttys000 · Terminal.app",
            "source": "god",
        }
    )
    assert "ttys000" in ans and "Terminal.app" in ans
    demo = spoken_session_answer(
        {"name": "Fake Build", "source": "demo", "session_hint": "ATC demo"}
    )
    assert "demo" in demo.lower()


def test_fast_path_session_tool():
    class FakeReg:
        def list_workers(self):
            return [
                Worker(
                    id="mcp-hands-50100",
                    name="MCP Hands · 50100",
                    status=WorkerStatus.RUNNING,
                    pid=50100,
                    source="god",
                    session_tty="ttys000",
                    session_app="Terminal.app",
                    session_hint="ttys000 · Terminal.app",
                )
            ]

        def resolve_id(self, name_or_id: str):
            if "mcp" in name_or_id.lower():
                return "mcp-hands-50100"
            return None

        def get(self, worker_id: str):
            for w in self.list_workers():
                if w.id == worker_id:
                    return w.snapshot()
            return None

    class Audit:
        def record(self, *a, **k):
            return None

    fleet = [w.snapshot() for w in FakeReg().list_workers()]
    out = _fast_fleet_action(
        "which terminal is mcp-hands in?",
        registry=FakeReg(),
        audit_log=Audit(),
        get_pending=lambda: None,
        set_pending=lambda x: None,
        confirm_phrase="confirm kill",
        source="text",
        fleet=fleet,
    )
    assert out is not None
    assert out.get("fast_path") is True
    assert out.get("action") == "session"
    assert "ttys000" in (out.get("spoken_reply") or "")


def test_spoken_from_tool_session():
    payload = {
        "ok": True,
        "action": "session",
        "result": {
            "summary": "MCP Hands is on ttys000 in Terminal.app.",
            "worker": {"name": "MCP Hands"},
        },
    }
    assert "ttys000" in _spoken_from_tool(payload)
