"""Kill harden: never collateral god/claude; mcp_hands leaf (+ uv parent only)."""

from __future__ import annotations

import os
import signal

import pytest

from backend.adapters.god_mode import (
    GodModeAdapter,
    Proc,
    cont_target_pids,
    is_mcp_hands_cmdline,
    kill_target_pids,
)

STACK = "/Users/simeong/local-claude-offline-stack"


def _proc(pid: int, cmd: str, ppid: int = 1, state: str = "S", tty: str = "ttys000") -> Proc:
    return Proc(pid=pid, ppid=ppid, state=state, command=cmd, user="simeong", tty=tty)


def test_is_mcp_hands_cmdline():
    assert is_mcp_hands_cmdline("python -m mcp_hands.server --profile quiet")
    assert is_mcp_hands_cmdline("uv run --project /x/mcp_hands python -m mcp_hands.server")
    assert not is_mcp_hands_cmdline(f"zsh {STACK}/god")
    assert not is_mcp_hands_cmdline("claude --resume 146e90ce-078b-4f95-9200-1a4d52322c0c")
    assert not is_mcp_hands_cmdline(f"{STACK}/god-rt watch")


def test_kill_targets_mcp_leaf_and_uv_parent_only():
    terminal = _proc(704, "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal", tty="??")
    zsh = _proc(835, "-zsh", ppid=704)
    god = _proc(900, f"zsh {STACK}/god", ppid=835)
    claude = _proc(
        901,
        "claude --resume 146e90ce-078b-4f95-9200-1a4d52322c0c",
        ppid=900,
    )
    uv = _proc(
        910,
        f"uv run --project {STACK}/mcp_hands python -m mcp_hands.server --profile quiet",
        ppid=835,
    )
    mcp = _proc(
        911,
        "python -m mcp_hands.server --profile quiet",
        ppid=910,
    )
    procs = [terminal, zsh, god, claude, uv, mcp]
    targets = kill_target_pids(mcp, procs)
    assert targets == [911, 910]
    assert 900 not in targets
    assert 901 not in targets
    assert 835 not in targets


def test_kill_targets_never_include_god_claude_even_if_parent():
    """If mcp were wrongly parented under claude, still never kill claude/god."""
    god = _proc(900, f"zsh {STACK}/god")
    claude = _proc(901, "claude --resume 146e90ce-078b-4f95-9200-1a4d52322c0c", ppid=900)
    mcp = _proc(911, "python -m mcp_hands.server --profile quiet", ppid=901)
    targets = kill_target_pids(mcp, [god, claude, mcp])
    assert targets == [911]
    assert 901 not in targets
    assert 900 not in targets


def test_cont_skips_wrapper_descendants():
    root = _proc(100, f"{STACK}/god-rt --live")
    child = _proc(101, "/bin/sleep 1", ppid=100)
    # Pathological: wrapper appearing under tree must be skipped
    wrapper = _proc(102, f"zsh {STACK}/god", ppid=100)
    claude = _proc(103, "claude --resume 146e90ce-078b-4f95-9200-1a4d52322c0c", ppid=102)
    got = cont_target_pids(100, [root, child, wrapper, claude])
    assert 100 in got and 101 in got
    assert 102 not in got and 103 not in got


def test_adapter_kill_mcp_signals_only_mcp_pids(monkeypatch, tmp_path):
    sent: list[tuple[int, int]] = []

    def fake_kill(pid, sig):
        sent.append((pid, sig))
        # pretend processes die after TERM
        return None

    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr("backend.adapters.god_mode.pid_exists", lambda pid: False)

    zsh = _proc(835, "-zsh")
    god = _proc(900, f"zsh {STACK}/god", ppid=835)
    claude = _proc(901, "claude --resume 146e90ce-078b-4f95-9200-1a4d52322c0c", ppid=900)
    uv = _proc(
        910,
        f"uv run --project {STACK}/mcp_hands python -m mcp_hands.server",
        ppid=835,
    )
    mcp = _proc(911, "python -m mcp_hands.server --profile quiet", ppid=910)
    adapter = GodModeAdapter(
        stack=STACK,
        include_demo=False,
        _procs=[zsh, god, claude, uv, mcp],
        _deny_pids=set(),
        targets_path=tmp_path / "t.json",
    )
    worker = adapter.kill("mcp-hands-911")
    assert worker.status.value == "killed"
    pids = {p for p, _ in sent}
    assert 911 in pids
    assert 910 in pids  # uv parent clearly mcp_hands
    assert 900 not in pids
    assert 901 not in pids
    assert 835 not in pids


def test_adapter_lists_wrapper_protected_explicit_kill_self_only(monkeypatch, tmp_path):
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr("backend.adapters.god_mode.pid_exists", lambda pid: False)
    child = _proc(901, "claude --resume 146e90ce-078b-4f95-9200-1a4d52322c0c", ppid=900)
    adapter = GodModeAdapter(
        stack=STACK,
        include_demo=False,
        _procs=[_proc(900, f"zsh {STACK}/god"), child],
        _deny_pids=set(),
        targets_path=tmp_path / "t.json",
    )
    workers = {w.id: w for w in adapter.list_workers()}
    assert "god-session-900" in workers
    assert workers["god-session-900"].protected is True
    assert workers["god-session-900"].session_hint
    w = adapter.kill("god-session-900")
    assert w.status.value == "killed"
    pids = {p for p, _ in sent}
    assert pids == {900}  # never collateral into claude child
