"""Hardening: fleet pause/resume/kill/steer must not hit live god/claude sessions.

All signals are mocked. No live SIGSTOP of real god sessions.
"""

from __future__ import annotations

import os
import signal

import pytest

from backend.adapters.composite import CompositeAdapter
from backend.adapters.demo import DemoAdapter
from backend.adapters.god_mode import GodModeAdapter, Proc
from backend.commands import parse_command
from backend.registry import Registry, Worker, WorkerStatus


STACK = "/Users/simeong/local-claude-offline-stack"


def _proc(pid: int, cmd: str, ppid: int = 1, state: str = "S") -> Proc:
    return Proc(pid=pid, ppid=ppid, state=state, command=cmd, user="simeong")


GOD_RT = _proc(12345, f"{STACK}/god-rt --live")
GOD_RT_CHILD = _proc(12346, "/usr/bin/sleep 99", ppid=12345)
GOD_LAUNCHER = _proc(25189, f"zsh {STACK}/god")
CLAUDE_CLI = _proc(
    25362,
    "claude --model claude-fable-auto --allow-dangerously-skip-permissions",
    ppid=25189,
)
MCP_UNDER = _proc(
    25668,
    "/opt/homebrew/opt/python@3.14/bin/python -m mcp_hands.server --profile quiet",
    ppid=25362,
)
CURSOR = _proc(400, "/Applications/Cursor.app/Contents/MacOS/Cursor")


class RecordingAdapter:
    """In-memory adapter that records pause/resume/kill and never signals."""

    def __init__(self, workers: list[Worker]) -> None:
        self._workers = {w.id: w for w in workers}
        self.paused: list[str] = []
        self.resumed: list[str] = []
        self.killed: list[str] = []

    def list_workers(self) -> list[Worker]:
        return list(self._workers.values())

    def _require(self, worker_id: str) -> str:
        if worker_id in self._workers:
            return worker_id
        for w in self._workers.values():
            if w.name.lower() == worker_id.lower() or str(w.pid) == worker_id:
                return w.id
        raise KeyError(f"unknown worker: {worker_id}")

    def pause(self, worker_id: str) -> Worker:
        wid = self._require(worker_id)
        self.paused.append(wid)
        w = self._workers[wid]
        w.status = WorkerStatus.PAUSED
        return w

    def resume(self, worker_id: str) -> Worker:
        wid = self._require(worker_id)
        self.resumed.append(wid)
        w = self._workers[wid]
        w.status = WorkerStatus.RUNNING
        return w

    def kill(self, worker_id: str) -> Worker:
        wid = self._require(worker_id)
        self.killed.append(wid)
        w = self._workers[wid]
        w.status = WorkerStatus.KILLED
        w.pid = None
        return w

    def redirect(self, worker_id: str, target: str) -> Worker:
        wid = self._require(worker_id)
        w = self._workers[wid]
        w.target = target
        return w

    def restart(self, worker_id: str) -> Worker:
        return self.resume(worker_id)


def _demo(wid: str, name: str, pid: int) -> Worker:
    return Worker(id=wid, name=name, status=WorkerStatus.RUNNING, pid=pid, source="demo")


def _god(wid: str, name: str, pid: int, **kw) -> Worker:
    return Worker(id=wid, name=name, status=WorkerStatus.RUNNING, pid=pid, source="god", **kw)


def test_parse_pause_all_is_demo_scope():
    cmd = parse_command("pause all")
    assert cmd and cmd.action == "pause_all" and cmd.scope == "demo"
    cmd = parse_command("hold the fleet")
    assert cmd and cmd.action == "pause_all" and cmd.scope == "demo"
    cmd = parse_command("resume all")
    assert cmd and cmd.action == "resume_all" and cmd.scope == "demo"


def test_parse_explicit_pause_god():
    cmd = parse_command("pause god")
    assert cmd and cmd.action == "pause" and cmd.worker_id == "god-rt"
    cmd = parse_command("pause god-rt")
    assert cmd and cmd.action == "pause" and cmd.worker_id == "god-rt"
    cmd = parse_command("pause all god")
    assert cmd and cmd.action == "pause_all" and cmd.scope == "god"
    cmd = parse_command("hold god fleet")
    assert cmd and cmd.action == "pause_all" and cmd.scope == "god"
    cmd = parse_command("resume all god")
    assert cmd and cmd.action == "resume_all" and cmd.scope == "god"


def test_pause_all_default_skips_god_and_session_trees():
    demo = _demo("log-spam", "Log Spam", 11)
    god_rt = _god(
        "god-rt-12345",
        "God RT",
        12345,
        cmdline_short=f"{STACK}/god-rt --live",
    )
    mcp = _god(
        "mcp-hands-25668",
        "MCP Hands",
        25668,
        cmdline_short="python -m mcp_hands.server --profile quiet",
        session_tree=True,
    )
    adapter = RecordingAdapter([demo, god_rt, mcp])
    reg = Registry(adapter)
    result = reg.pause_all()
    assert result["scope"] == "demo"
    assert result["paused_count"] == 1
    assert adapter.paused == ["log-spam"]
    reasons = {s["id"]: s["reason"] for s in result["skipped"]}
    assert "god-rt-12345" in reasons
    assert "excluded" in reasons["god-rt-12345"] or "god" in reasons["god-rt-12345"]
    assert "mcp-hands-25668" in reasons
    assert "session tree" in reasons["mcp-hands-25668"]


def test_pause_all_god_skips_wrappers_and_session_trees_not_demo():
    demo = _demo("fake-build", "Fake Build", 12)
    god_rt = _god(
        "god-rt-12345",
        "God RT",
        12345,
        cmdline_short=f"{STACK}/god-rt --live",
    )
    wrapper = _god(
        "god-25189",
        "God",
        25189,
        cmdline_short=f"zsh {STACK}/god",
        detail=f"[S] zsh {STACK}/god",
    )
    mcp = _god(
        "mcp-hands-25668",
        "MCP Hands",
        25668,
        session_tree=True,
        cmdline_short="python -m mcp_hands.server",
    )
    adapter = RecordingAdapter([demo, god_rt, wrapper, mcp])
    reg = Registry(adapter)
    result = reg.pause_all(scope="god")
    assert result["scope"] == "god"
    assert adapter.paused == ["god-rt-12345"]
    skipped_ids = {s["id"] for s in result["skipped"]}
    assert "fake-build" in skipped_ids
    assert "god-25189" in skipped_ids  # protected wrapper
    assert "mcp-hands-25668" in skipped_ids  # session tree


def test_explicit_pause_god_rt_allowed_via_adapter(monkeypatch):
    sent: list[tuple[int, int]] = []

    def fake_kill(pid, sig):
        sent.append((pid, sig))

    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr("backend.adapters.god_mode.proc_state", lambda pid, user="simeong": "T")
    adapter = GodModeAdapter(
        stack=STACK,
        include_demo=False,
        _procs=[GOD_RT, GOD_LAUNCHER, CLAUDE_CLI],
        _deny_pids=set(),
    )
    worker = adapter.pause("god-rt-12345")
    assert worker.status == WorkerStatus.PAUSED
    assert sent == [(12345, signal.SIGSTOP)]
    # wrappers never become pause targets
    with pytest.raises(KeyError):
        adapter.pause("25189")
    with pytest.raises(KeyError):
        adapter._require("claude")


def test_resume_conts_whole_tree(monkeypatch):
    sent: list[tuple[int, int]] = []

    def fake_kill(pid, sig):
        sent.append((pid, sig))

    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr("backend.adapters.god_mode.proc_state", lambda pid, user="simeong": "S")
    paused = _proc(12345, f"{STACK}/god-rt --live", state="T")
    adapter = GodModeAdapter(
        stack=STACK,
        include_demo=False,
        _procs=[paused, GOD_RT_CHILD],
        _deny_pids=set(),
    )
    worker = adapter.resume("god-rt-12345")
    assert worker.status == WorkerStatus.RUNNING
    assert (12345, signal.SIGCONT) in sent
    assert (12346, signal.SIGCONT) in sent
    assert all(sig == signal.SIGCONT for _, sig in sent)


def test_pause_wrapper_refused_even_if_injected(monkeypatch):
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    # Force a wrapper into the override list; classify still denies it
    adapter = GodModeAdapter(
        stack=STACK,
        include_demo=False,
        _procs=[GOD_LAUNCHER],
        _deny_pids=set(),
    )
    with pytest.raises(KeyError):
        adapter.pause("25189")
    assert sent == []


def test_kill_refuses_bare_god_and_protected_wrapper():
    god_rt = _god("god-rt-12345", "God RT", 12345, cmdline_short=f"{STACK}/god-rt --live")
    wrapper = _god(
        "god-25189",
        "God",
        25189,
        cmdline_short=f"zsh {STACK}/god",
        detail=f"[S] zsh {STACK}/god",
    )
    cursor = _god(
        "cursor-400",
        "Cursor",
        400,
        cmdline_short="/Applications/Cursor.app/Contents/MacOS/Cursor",
        detail="[/Applications/Cursor.app/Contents/MacOS/Cursor]",
    )
    mcp = _god(
        "mcp-hands-25668",
        "MCP Hands",
        25668,
        cmdline_short="python -m mcp_hands.server --profile quiet",
    )
    demo = _demo("log-spam", "Log Spam", 11)
    adapter = RecordingAdapter([god_rt, wrapper, cursor, mcp, demo])
    reg = Registry(adapter)

    with pytest.raises(PermissionError, match="protected"):
        reg.request_kill("god")  # name-matches wrapper "God"
    with pytest.raises(PermissionError, match="protected"):
        reg.request_kill("god-25189")
    # Bare "god" against a lone god-rt workload still requires an explicit id
    lone = RecordingAdapter([
        _god("god-rt-12345", "God RT", 12345, cmdline_short=f"{STACK}/god-rt --live")
    ])
    with pytest.raises(PermissionError, match="explicit id"):
        Registry(lone).request_kill("god")
    with pytest.raises(PermissionError, match="protected"):
        reg.request_kill("cursor-400")

    armed = reg.request_kill("mcp-hands-25668")
    assert armed["armed"] is True
    armed_demo = reg.request_kill("log-spam")
    assert armed_demo["armed"] is True
    # confirm still required — execute without arm already tested elsewhere
    result = reg.execute_kill("log-spam", "confirm kill")
    assert result["after"]["status"] == "killed"
    assert adapter.killed == ["log-spam"]
    # mcp armed but not confirmed
    assert "mcp-hands-25668" not in adapter.killed


def test_kill_confirm_does_not_hit_wrong_target():
    demo = _demo("fake-research", "Fake Research", 13)
    god_rt = _god("god-rt-12345", "God RT", 12345, cmdline_short=f"{STACK}/god-rt --live")
    adapter = RecordingAdapter([demo, god_rt])
    reg = Registry(adapter)
    reg.request_kill("fake-research")
    result = reg.execute_kill("fake-research", "confirm kill")
    assert result["after"]["id"] == "fake-research"
    assert adapter.killed == ["fake-research"]
    assert "god-rt-12345" not in adapter.killed


def test_redirect_does_not_signal(monkeypatch, tmp_path):
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))

    def runner(cmd, timeout):
        import subprocess

        return subprocess.CompletedProcess(cmd, 0, stdout="BRIEF ok\n", stderr="")

    adapter = GodModeAdapter(
        stack=STACK,
        include_demo=False,
        _procs=[GOD_RT],
        _deny_pids=set(),
        targets_path=tmp_path / "t.json",
        _runner=runner,
    )
    worker = adapter.redirect("god-rt-12345", "brief")
    assert worker.target == "brief"
    assert "steered" in (worker.detail or "")
    assert sent == []


def test_redirect_still_denies_engage(tmp_path):
    adapter = GodModeAdapter(
        stack=STACK,
        include_demo=False,
        _procs=[GOD_RT],
        _deny_pids=set(),
        targets_path=tmp_path / "t.json",
        _runner=lambda c, t: None,
    )
    with pytest.raises(RuntimeError, match="denied"):
        adapter.redirect("god-rt", "engage")


def test_hybrid_pause_all_does_not_call_god_pause(monkeypatch, tmp_path):
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    god = GodModeAdapter(
        stack=STACK,
        include_demo=False,
        _procs=[GOD_RT, GOD_LAUNCHER, CLAUDE_CLI, MCP_UNDER],
        _deny_pids=set(),
        targets_path=tmp_path / "t.json",
    )
    demo = DemoAdapter()
    # Don't start real demo processes — empty demo list
    hybrid = CompositeAdapter(demo, god)
    reg = Registry(hybrid)
    result = reg.pause_all()
    assert result["paused_count"] == 0
    assert sent == []
    # god-rt skipped as non-demo; wrappers not listed; mcp is session-tree
    skipped_ids = {s["id"] for s in result["skipped"]}
    assert "god-rt-12345" in skipped_ids
    listed = {w["id"] for w in reg.list_workers()}
    assert "god-rt-12345" in listed
    assert not any("25189" in i or i.startswith("claude") for i in listed)


def test_single_pause_mcp_hands_allowed(monkeypatch):
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr("backend.adapters.god_mode.proc_state", lambda pid, user="simeong": "T")
    adapter = GodModeAdapter(
        stack=STACK,
        include_demo=False,
        _procs=[GOD_LAUNCHER, CLAUDE_CLI, MCP_UNDER],
        _deny_pids=set(),
    )
    worker = adapter.pause("mcp-hands-25668")
    assert worker.status == WorkerStatus.PAUSED
    assert sent == [(25668, signal.SIGSTOP)]
