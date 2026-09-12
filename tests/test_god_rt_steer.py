"""God Mode quiet steer path — allowlist + mocked god-rt runner."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.adapters.god_mode import GodModeAdapter, Proc
from backend.adapters.god_rt_steer import (
    parse_steer_target,
    run_quiet_steer,
    summarize_output,
)
from backend.registry import WorkerStatus


STACK = "/Users/simeong/local-claude-offline-stack"


def _proc(pid: int = 12345, state: str = "S") -> Proc:
    return Proc(
        pid=pid,
        ppid=1,
        state=state,
        command=f"{STACK}/god-rt --live",
        user="simeong",
    )


def test_parse_allowlisted_verbs():
    assert parse_steer_target("brief").verb == "brief"
    assert parse_steer_target("campaign_status").argv == ["campaign_status"]
    assert parse_steer_target("campaign status").verb == "campaign_status"
    assert parse_steer_target("list").argv == ["campaign_list"]
    assert parse_steer_target("ready").verb == "ready"
    assert parse_steer_target("next").verb == "next"
    assert parse_steer_target("probe").argv == ["campaign_status"]
    assert parse_steer_target("brief zeeh.africa").argv == ["brief", "zeeh.africa"]


def test_parse_label_only_and_deny():
    assert parse_steer_target("docs backlog") is None
    with pytest.raises(ValueError, match="denied"):
        parse_steer_target("engage")
    with pytest.raises(ValueError, match="denied"):
        parse_steer_target("exploit_auto target.com")
    with pytest.raises(ValueError, match="denied"):
        parse_steer_target("go AUTH")


def test_summarize_prefers_brief_line():
    text = "[opsec_down] x\nBRIEF  target=x keep=2\nSTATUS phase=hunt\n"
    assert summarize_output(text).startswith("BRIEF")


def test_run_quiet_steer_mocked_ok():
    def runner(cmd, timeout):
        assert cmd[0].endswith("god-rt")
        assert cmd[1:] == ["brief"]
        return subprocess.CompletedProcess(
            cmd, 0, stdout="BRIEF  target=demo keep=1 kill=0\n", stderr=""
        )

    # Pretend binary exists by pointing stack at a temp dir with god-rt file
    # run_quiet_steer checks is_file — use real stack binary path via monkeypatch of Path
    result = run_quiet_steer("brief", stack=STACK, runner=runner)
    # Real binary exists on Simeon's Mac
    assert result.ok
    assert result.verb == "brief"
    assert "BRIEF" in result.summary


def test_run_quiet_steer_missing_binary(tmp_path):
    result = run_quiet_steer("brief", stack=str(tmp_path), runner=lambda c, t: None)
    assert not result.ok
    assert "not found" in (result.error or "")


def test_run_quiet_steer_label_only():
    result = run_quiet_steer("docs backlog", stack=STACK)
    assert result.ok
    assert result.verb is None
    assert result.summary == "label-only"


def test_adapter_redirect_steers_god_rt(tmp_path):
    calls: list[list[str]] = []

    def runner(cmd, timeout):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, stdout="BRIEF  target=demo keep=3\n", stderr=""
        )

    adapter = GodModeAdapter(
        stack=STACK,
        include_demo=False,
        _procs=[_proc()],
        _deny_pids=set(),
        targets_path=tmp_path / "god-targets.json",
        _runner=runner,
    )
    worker = adapter.redirect("god-rt", "brief")
    assert worker.target == "brief"
    assert "steered" in (worker.detail or "")
    assert worker.status == WorkerStatus.RUNNING
    steer = adapter.pop_last_steer()
    assert steer and steer["ok"] and steer["verb"] == "brief"
    assert calls and calls[0][1:] == ["brief"]


def test_adapter_redirect_denies_engage(tmp_path):
    adapter = GodModeAdapter(
        stack=STACK,
        include_demo=False,
        _procs=[_proc()],
        _deny_pids=set(),
        targets_path=tmp_path / "t.json",
        _runner=lambda c, t: subprocess.CompletedProcess(c, 0, "", ""),
    )
    with pytest.raises(RuntimeError, match="denied"):
        adapter.redirect("god-rt", "engage")


def test_adapter_redirect_honest_error_when_runner_fails(tmp_path):
    def runner(cmd, timeout):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    adapter = GodModeAdapter(
        stack=STACK,
        include_demo=False,
        _procs=[_proc()],
        _deny_pids=set(),
        targets_path=tmp_path / "t.json",
        _runner=runner,
    )
    with pytest.raises(RuntimeError):
        adapter.redirect("god-rt", "campaign_status")


def test_non_god_rt_stays_label_only(tmp_path):
    mcp = Proc(
        pid=1317,
        ppid=1,
        state="S",
        command="/opt/homebrew/bin/python -m mcp_hands.server --profile quiet",
        user="simeong",
    )
    called = []

    def runner(cmd, timeout):
        called.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "x", "")

    adapter = GodModeAdapter(
        stack=STACK,
        include_demo=False,
        _procs=[mcp],
        _deny_pids=set(),
        targets_path=tmp_path / "t.json",
        _runner=runner,
    )
    worker = adapter.redirect("mcp-hands-1317", "brief")
    assert worker.target == "brief"
    assert "redirected" in (worker.detail or "")
    assert not called
