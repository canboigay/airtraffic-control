"""Confirm-gate and command parsing tests."""

from __future__ import annotations

from backend.commands import parse_command
from backend.registry import CONFIRM_PHRASE


def test_parse_status():
    cmd = parse_command("status")
    assert cmd and cmd.action == "status"


def test_parse_pause_worker():
    cmd = parse_command("pause fake build")
    assert cmd and cmd.action == "pause"
    assert cmd.worker_id == "fake-build"


def test_parse_kill_and_confirm():
    kill = parse_command("kill log spam")
    assert kill and kill.action == "kill" and kill.worker_id == "log-spam"
    conf = parse_command("confirm kill")
    assert conf and conf.action == "confirm_kill"
    assert conf.confirm_text == CONFIRM_PHRASE


def test_parse_redirect():
    cmd = parse_command("redirect research to docs")
    assert cmd and cmd.action == "redirect"
    assert cmd.worker_id == "fake-research"
    assert cmd.target == "docs"


def test_parse_resume():
    cmd = parse_command("resume fake-build")
    assert cmd and cmd.action == "resume"
    assert cmd.worker_id == "fake-build"


def test_parse_steer_god_rt():
    cmd = parse_command("steer god-rt to brief")
    assert cmd and cmd.action == "redirect"
    assert cmd.worker_id == "god-rt"
    assert cmd.target == "brief"
