"""God Mode adapter discovery, denylist, hybrid routing, SIGSTOP."""

from __future__ import annotations

import os
import signal
import subprocess
import time

import pytest

from backend.adapters.composite import CompositeAdapter, adapter_mode, build_fleet
from backend.adapters.demo import DemoAdapter
from backend.adapters.god_mode import (
    GodModeAdapter,
    Proc,
    ancestor_is_protected_session,
    assign_ids,
    classify_proc,
    descendant_pids,
    human_name,
    is_denied_cmdline,
    is_god_session_wrapper,
    proc_state,
    select_workers,
    slug_for,
)
from backend.registry import Registry, WorkerStatus

STACK = "/Users/simeong/local-claude-offline-stack"


def _proc(pid: int, cmd: str, ppid: int = 1, state: str = "S") -> Proc:
    return Proc(pid=pid, ppid=ppid, state=state, command=cmd, user="simeong")


CLAUDE_HELPER = _proc(
    1309,
    "/Applications/Claude.app/Contents/Helpers/disclaimer --pgroup -- "
    f"{STACK}/bin/god-mcp-hands --profile quiet",
)
UV_PARENT = _proc(
    1315,
    f"/Users/simeong/.local/bin/uv run --project {STACK}/mcp_hands python -m mcp_hands.server --profile quiet",
    ppid=1314,
)
MCP_CHILD = _proc(
    1317,
    "/opt/homebrew/opt/python@3.14/bin/python -m mcp_hands.server --profile quiet",
    ppid=1315,
)
GOD_RT = _proc(12345, f"{STACK}/god-rt --live")
GOD_WATCH = _proc(222, f"/usr/bin/python3 {STACK}/god_watch.py")
CSUPER = _proc(333, f"/usr/bin/python3 {STACK}/campaign-supervisor/csuper/__main__.py")
LOG_SPAM = _proc(10665, "/usr/bin/python3 /Users/simeong/Projects/airtraffic-control/backend/workers/log_spam.py")
UVICORN = _proc(10642, "Python -m uvicorn backend.main:app --host 127.0.0.1 --port 8765")
CURSOR = _proc(400, "/Applications/Cursor.app/Contents/MacOS/Cursor")
LOGIN = _proc(401, "/System/Library/CoreServices/loginwindow.app/Contents/MacOS/loginwindow")
SCANNER = _proc(402, f"rg -n mcp_hands {STACK}")
PORT_8088 = _proc(80881, f"python {STACK}/god_proxy_handler.py")
GOD_LAUNCHER = _proc(25189, f"zsh {STACK}/god")
CLAUDE_CLI = _proc(
    25362,
    "claude --model claude-fable-auto --allow-dangerously-skip-permissions "
    f"--settings={STACK}/profiles/quiet.json",
    ppid=25189,
)
MCP_UNDER_GOD = _proc(
    25668,
    "/opt/homebrew/opt/python@3.14/bin/python -m mcp_hands.server --profile quiet",
    ppid=25362,
)


def test_adapter_mode_defaults_hybrid(monkeypatch):
    monkeypatch.delenv("ATC_ADAPTER", raising=False)
    assert adapter_mode() == "hybrid"
    monkeypatch.setenv("ATC_ADAPTER", "local")
    assert adapter_mode() == "local"
    monkeypatch.setenv("ATC_ADAPTER", "demo")
    assert adapter_mode() == "demo"


def test_denylist_cmdlines():
    assert is_denied_cmdline(CLAUDE_HELPER.command)
    assert is_denied_cmdline(UVICORN.command)
    assert is_denied_cmdline(CURSOR.command)
    assert is_denied_cmdline(LOGIN.command)
    # god/claude TUI wrappers are shown as protected sessions — not on the GUI denylist
    assert not is_denied_cmdline(GOD_LAUNCHER.command)
    assert not is_denied_cmdline(CLAUDE_CLI.command)
    assert is_god_session_wrapper(GOD_LAUNCHER.command)
    assert is_god_session_wrapper(CLAUDE_CLI.command)
    assert not is_god_session_wrapper(GOD_RT.command)
    assert not is_god_session_wrapper(GOD_WATCH.command)
    assert not is_denied_cmdline(MCP_CHILD.command)
    assert not is_denied_cmdline(GOD_RT.command)


def test_select_keeps_leaf_mcp_and_stack_workloads():
    procs = [
        CLAUDE_HELPER, UV_PARENT, MCP_CHILD, GOD_RT, GOD_WATCH, CSUPER, LOG_SPAM,
        UVICORN, CURSOR, LOGIN, SCANNER, GOD_LAUNCHER, CLAUDE_CLI, MCP_UNDER_GOD,
    ]
    selected = select_workers(procs, deny_pids={10642, 80881}, include_demo=False, stack=STACK)
    pids = {p.pid for p in selected}
    assert 1317 in pids
    assert 1315 not in pids  # parent dropped in favor of python child
    assert 12345 in pids
    assert 222 in pids
    assert 333 in pids
    assert 1309 not in pids
    assert 10642 not in pids
    assert 10665 not in pids  # demo excluded
    assert 400 not in pids
    assert 401 not in pids
    assert 402 not in pids
    assert 25189 in pids  # zsh …/god shown as protected session
    assert 25362 in pids  # claude CLI shown as protected session
    assert 25668 in pids  # mcp-hands under session is listed but fleet-pause skipped


def test_select_include_demo_and_port_denylist():
    selected = select_workers(
        [LOG_SPAM, PORT_8088, MCP_CHILD],
        deny_pids={80881},
        include_demo=True,
        stack=STACK,
    )
    pids = {p.pid for p in selected}
    assert 10665 in pids
    assert 1317 in pids
    assert 80881 not in pids


def test_ids_and_names():
    pairs = assign_ids([MCP_CHILD, GOD_RT], stack=STACK)
    ids = {wid: proc.pid for wid, proc in pairs}
    assert "mcp-hands-1317" in ids
    assert "god-rt-12345" in ids
    assert human_name("mcp-hands", MCP_CHILD.command) == "MCP Hands (quiet)"
    assert slug_for(GOD_WATCH, stack=STACK) == "god-watch"


def test_require_fuzzy_and_ambiguous():
    other = _proc(1313, "/opt/homebrew/opt/python@3.14/bin/python -m mcp_hands.server --profile quiet", ppid=1)
    adapter = GodModeAdapter(
        stack=STACK,
        include_demo=False,
        _procs=[MCP_CHILD, other, GOD_RT],
        _deny_pids=set(),
    )
    assert adapter._require("god-rt-12345") == "god-rt-12345"
    assert adapter._require("God RT") == "god-rt-12345"
    assert adapter._require("12345") == "god-rt-12345"
    with pytest.raises(KeyError, match="ambiguous"):
        adapter._require("mcp-hands")
    assert adapter._require("mcp-hands-1317") == "mcp-hands-1317"


def test_restart_discovered_running_errors():
    adapter = GodModeAdapter(
        stack=STACK,
        include_demo=False,
        _procs=[GOD_RT],
        _deny_pids=set(),
    )
    with pytest.raises(RuntimeError, match="discovered process"):
        adapter.restart("god-rt-12345")


def test_restart_discovered_paused_resumes(tmp_path, monkeypatch):
    paused = _proc(12345, f"{STACK}/god-rt --live", state="T")
    adapter = GodModeAdapter(
        stack=STACK,
        include_demo=False,
        _procs=[paused],
        _deny_pids=set(),
        targets_path=tmp_path / "god-targets.json",
    )

    def _cont(pid, sig):
        assert pid == 12345
        assert sig == signal.SIGCONT

    monkeypatch.setattr(os, "kill", _cont)
    monkeypatch.setattr("backend.adapters.god_mode.proc_state", lambda pid, user="simeong": "S")
    worker = adapter.restart("god-rt")
    assert worker.status == WorkerStatus.RUNNING


def test_redirect_persists_json(tmp_path):
    path = tmp_path / "god-targets.json"
    adapter = GodModeAdapter(
        stack=STACK,
        include_demo=False,
        _procs=[GOD_RT],
        _deny_pids=set(),
        targets_path=path,
    )
    worker = adapter.redirect("god-rt", "docs backlog")
    assert worker.target == "docs backlog"
    listed = adapter.list_workers()
    assert listed[0].target == "docs backlog"
    raw = path.read_text()
    assert "docs backlog" in raw


def test_describe_is_live():
    adapter = GodModeAdapter(stack=STACK, include_demo=False, _procs=[GOD_RT], _deny_pids=set())
    info = adapter.describe_interface()
    assert info["status"] == "live"
    assert info["worker_count"] == 1
    assert "list_workers" in info["methods"]


def test_composite_hybrid_lists_demo_and_god(tmp_path):
    demo = DemoAdapter()
    # Don't start real demo processes — inject via god only + empty demo meta
    god = GodModeAdapter(
        stack=STACK,
        include_demo=False,
        _procs=[MCP_CHILD, LOG_SPAM],
        _deny_pids=set(),
        targets_path=tmp_path / "t.json",
    )
    hybrid = CompositeAdapter(demo, god)
    ids = {w.id for w in hybrid.list_workers()}
    assert "mcp-hands-1317" in ids
    assert "log-spam" not in ids  # demo adapter not started; god include_demo=False


def test_build_fleet_modes(monkeypatch):
    monkeypatch.setenv("ATC_ADAPTER", "local")
    fleet = build_fleet()
    assert fleet.mode == "local"
    assert fleet.demo is None
    assert isinstance(fleet.adapter, GodModeAdapter)
    monkeypatch.setenv("ATC_ADAPTER", "demo")
    fleet = build_fleet()
    assert fleet.mode == "demo"
    assert fleet.god is None
    assert isinstance(fleet.adapter, DemoAdapter)
    monkeypatch.setenv("ATC_ADAPTER", "hybrid")
    fleet = build_fleet()
    assert fleet.mode == "hybrid"
    assert isinstance(fleet.adapter, CompositeAdapter)
    assert fleet.demo is not None and fleet.god is not None
    assert fleet.god.include_demo is False


def test_sigstop_sigcont_roundtrip():
    proc = subprocess.Popen(["sleep", "20"])
    try:
        os.kill(proc.pid, signal.SIGSTOP)
        time.sleep(0.1)
        state = proc_state(proc.pid)
        assert state and state[0].upper() == "T"
        os.kill(proc.pid, signal.SIGCONT)
        time.sleep(0.1)
        state = proc_state(proc.pid)
        assert state and state[0].upper() != "T"
    finally:
        proc.kill()
        proc.wait(timeout=2)


def test_live_adapter_lists_without_requiring_demo():
    adapter = GodModeAdapter(include_demo=True)
    workers = adapter.list_workers()
    assert isinstance(workers, list)
    for w in workers:
        assert w.id
        assert w.pid
        assert "uvicorn" not in (w.detail or "").lower()
        assert "Claude.app" not in (w.detail or "")
        cmd = w.cmdline_short or w.detail or ""
        # GUI denylist never listed; god/claude wrappers may be listed as protected
        assert is_denied_cmdline(cmd) is False
        if is_god_session_wrapper(cmd):
            assert w.protected is True
            assert w.source == "session"


def test_session_tree_and_descendants():
    helper = _proc(201, "/usr/bin/sleep 9", ppid=12345)
    assert descendant_pids([GOD_RT, helper], 12345) == [201]
    assert ancestor_is_protected_session(MCP_UNDER_GOD, [GOD_LAUNCHER, CLAUDE_CLI, MCP_UNDER_GOD])
    assert not ancestor_is_protected_session(GOD_RT, [GOD_RT, helper])


def test_list_workers_does_not_signal(monkeypatch):
    calls = []

    def boom(pid, sig):
        calls.append((pid, sig))
        raise AssertionError("discovery must not signal")

    monkeypatch.setattr(os, "kill", boom)
    adapter = GodModeAdapter(
        stack=STACK,
        include_demo=False,
        _procs=[GOD_RT, GOD_LAUNCHER, CLAUDE_CLI, MCP_UNDER_GOD],
        _deny_pids=set(),
    )
    workers = adapter.list_workers()
    ids = {w.id for w in workers}
    assert "god-rt-12345" in ids
    assert "god-session-25189" in ids
    assert "claude-session-25362" in ids
    by = {w.id: w for w in workers}
    assert by["god-session-25189"].protected is True
    assert by["claude-session-25362"].protected is True
    under = [w for w in workers if w.pid == 25668]
    assert under and under[0].session_tree is True
    assert calls == []
