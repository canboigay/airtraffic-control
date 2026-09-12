"""Discover terminal Grok CLI / Gemini CLI without false positives."""

from __future__ import annotations

import os
import signal

import pytest

from backend.adapters.god_mode import (
    GodModeAdapter,
    Proc,
    _cli_hit,
    classify_proc,
    human_name,
    is_denied_cmdline,
    select_workers,
    slug_for,
)
from backend.commands import parse_command
from backend.registry import Registry, Worker, WorkerStatus

STACK = "/Users/simeong/local-claude-offline-stack"


def _proc(pid: int, cmd: str, ppid: int = 1, state: str = "S") -> Proc:
    return Proc(pid=pid, ppid=ppid, state=state, command=cmd, user="simeong")


GROK_LOCAL = _proc(50100, "/Users/simeong/.local/bin/grok")
GROK_DOT = _proc(50101, "/Users/simeong/.grok/bin/grok chat")
GROK_NPM = _proc(50102, "/Users/simeong/.npm-global/bin/grok")
GEMINI_BREW = _proc(50200, "/opt/homebrew/bin/gemini")
GEMINI_NODE = _proc(50201, "node /opt/homebrew/bin/gemini")
GEMINI_JS = _proc(
    50202,
    "node /opt/homebrew/lib/node_modules/@google/gemini-cli/bundle/gemini.js",
)
GEMINI_USR = _proc(50203, "/usr/local/bin/gemini --help")

# False positives — must NEVER match
GROK_BOT = _proc(697, "/Applications/Grok Bot.app/Contents/MacOS/Grok Bot")
GROK_HELPER = _proc(
    1025,
    "/Applications/Grok Bot.app/Contents/Frameworks/Grok Bot Helper.app/"
    "Contents/MacOS/Grok Bot Helper --type=gpu-process",
)
PROGROK = _proc(25006, "node /opt/homebrew/bin/progrok proxy --host 127.0.0.1 --port 18645")
NGROK = _proc(25007, "/usr/local/bin/ngrok http 8080")
GROK_BOT_LOWER = _proc(25008, " somehow grok bot helper leftover")


def test_cli_hit_exact_basenames():
    assert _cli_hit(GROK_LOCAL.command) == "grok"
    assert _cli_hit(GROK_DOT.command) == "grok"
    assert _cli_hit(GROK_NPM.command) == "grok"
    assert _cli_hit(GEMINI_BREW.command) == "gemini"
    assert _cli_hit(GEMINI_NODE.command) == "gemini"
    assert _cli_hit(GEMINI_JS.command) == "gemini"
    assert _cli_hit(GEMINI_USR.command) == "gemini"


def test_cli_hit_rejects_false_positives():
    assert _cli_hit(GROK_BOT.command) is None
    assert _cli_hit(GROK_HELPER.command) is None
    assert _cli_hit(PROGROK.command) is None
    assert _cli_hit(NGROK.command) is None
    assert _cli_hit("progrok") is None
    assert _cli_hit("ngrok") is None
    assert _cli_hit("/Applications/Grok Bot.app/Contents/MacOS/Grok Bot") is None


def test_gui_denylist_still_covers_grok_bot():
    assert is_denied_cmdline(GROK_BOT.command)
    assert is_denied_cmdline(GROK_HELPER.command)
    assert is_denied_cmdline(GROK_BOT_LOWER.command)
    assert not is_denied_cmdline(GROK_LOCAL.command)
    assert not is_denied_cmdline(GEMINI_BREW.command)


def test_classify_and_slug_names():
    assert classify_proc(GROK_LOCAL, stack=STACK, include_demo=False)
    assert classify_proc(GEMINI_JS, stack=STACK, include_demo=False)
    assert not classify_proc(PROGROK, stack=STACK, include_demo=False)
    assert not classify_proc(NGROK, stack=STACK, include_demo=False)
    assert not classify_proc(GROK_BOT, stack=STACK, include_demo=False)
    assert slug_for(GROK_LOCAL, stack=STACK) == "grok-cli"
    assert slug_for(GEMINI_BREW, stack=STACK) == "gemini-cli"
    assert human_name("grok-cli", GROK_LOCAL.command, 50100) == "Grok CLI · 50100"
    assert human_name("gemini-cli", GEMINI_BREW.command, 50200) == "Gemini CLI · 50200"


def test_select_discovers_cli_not_false_positives():
    procs = [
        GROK_LOCAL,
        GEMINI_BREW,
        GEMINI_NODE,
        GROK_BOT,
        GROK_HELPER,
        PROGROK,
        NGROK,
        _proc(12345, f"{STACK}/god-rt --live"),
    ]
    selected = select_workers(procs, deny_pids=set(), include_demo=False, stack=STACK)
    pids = {p.pid for p in selected}
    assert 50100 in pids
    assert 50200 in pids or 50201 in pids  # leaf preference may keep one gemini
    assert 697 not in pids
    assert 1025 not in pids
    assert 25006 not in pids
    assert 25007 not in pids
    assert 12345 in pids


def test_list_workers_source_cli(tmp_path):
    adapter = GodModeAdapter(
        stack=STACK,
        include_demo=False,
        _procs=[GROK_LOCAL, GEMINI_BREW, GROK_BOT, PROGROK, NGROK],
        _deny_pids=set(),
        targets_path=tmp_path / "t.json",
    )
    workers = adapter.list_workers()
    by_id = {w.id: w for w in workers}
    assert "grok-cli-50100" in by_id
    assert "gemini-cli-50200" in by_id
    assert by_id["grok-cli-50100"].source == "cli"
    assert by_id["gemini-cli-50200"].source == "cli"
    assert by_id["grok-cli-50100"].name == "Grok CLI · 50100"
    assert by_id["gemini-cli-50200"].name == "Gemini CLI · 50200"
    # no false positives
    assert all("Grok Bot" not in (w.name or "") for w in workers)
    assert all(w.pid not in {697, 25006, 25007} for w in workers)


def test_pause_all_demo_skips_cli():
    """pause_all stays demo-only; CLI workers need explicit pause <id>."""

    class RecordingAdapter:
        def __init__(self, workers):
            self._workers = workers
            self.paused: list[str] = []

        def list_workers(self):
            return list(self._workers)

        def pause(self, worker_id: str):
            self.paused.append(worker_id)
            for w in self._workers:
                if w.id == worker_id:
                    w.status = WorkerStatus.PAUSED
                    return w
            raise KeyError(worker_id)

        def resume(self, worker_id: str):
            raise NotImplementedError

        def kill(self, worker_id: str):
            raise NotImplementedError

        def redirect(self, worker_id: str, target: str):
            raise NotImplementedError

        def restart(self, worker_id: str):
            raise NotImplementedError

    demo = Worker(id="log-spam", name="Log Spam", status=WorkerStatus.RUNNING, pid=11, source="demo")
    cli = Worker(
        id="grok-cli-50100",
        name="Grok CLI · 50100",
        status=WorkerStatus.RUNNING,
        pid=50100,
        source="cli",
        cmdline_short="/Users/simeong/.local/bin/grok",
    )
    god = Worker(
        id="god-rt-12345",
        name="God RT",
        status=WorkerStatus.RUNNING,
        pid=12345,
        source="god",
        cmdline_short=f"{STACK}/god-rt --live",
    )
    adapter = RecordingAdapter([demo, cli, god])
    reg = Registry(adapter)
    result = reg.pause_all()
    assert result["scope"] == "demo"
    assert adapter.paused == ["log-spam"]
    skipped = {s["id"]: s["reason"] for s in result["skipped"]}
    assert "grok-cli-50100" in skipped
    assert "excluded" in skipped["grok-cli-50100"] or "cli" in skipped["grok-cli-50100"]


def test_explicit_pause_cli_sigstops_pid_only(monkeypatch, tmp_path):
    sent: list[tuple[int, int]] = []

    def fake_kill(pid, sig):
        sent.append((pid, sig))

    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr("backend.adapters.god_mode.proc_state", lambda pid, user="simeong": "T")
    child = _proc(50199, "/bin/sleep 1", ppid=50100)
    adapter = GodModeAdapter(
        stack=STACK,
        include_demo=False,
        _procs=[GROK_LOCAL, child],
        _deny_pids=set(),
        targets_path=tmp_path / "t.json",
    )
    worker = adapter.pause("grok-cli-50100")
    assert worker.status == WorkerStatus.PAUSED
    assert worker.source == "cli"
    # PID only — never the unrelated/descendant tree on pause
    assert sent == [(50100, signal.SIGSTOP)]


def test_resume_cli_cont_tree(monkeypatch, tmp_path):
    sent: list[tuple[int, int]] = []

    def fake_kill(pid, sig):
        sent.append((pid, sig))

    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr("backend.adapters.god_mode.proc_state", lambda pid, user="simeong": "S")
    paused = _proc(50100, "/Users/simeong/.local/bin/grok", state="T")
    child = _proc(50199, "/bin/sleep 1", ppid=50100, state="T")
    adapter = GodModeAdapter(
        stack=STACK,
        include_demo=False,
        _procs=[paused, child],
        _deny_pids=set(),
        targets_path=tmp_path / "t.json",
    )
    worker = adapter.resume("grok-cli-50100")
    assert worker.status == WorkerStatus.RUNNING
    assert (50100, signal.SIGCONT) in sent
    assert (50199, signal.SIGCONT) in sent


def test_kill_confirm_arms_cli():
    class RecordingAdapter:
        def __init__(self, workers):
            self._workers = workers

        def list_workers(self):
            return list(self._workers)

        def pause(self, worker_id: str):
            raise NotImplementedError

        def resume(self, worker_id: str):
            raise NotImplementedError

        def kill(self, worker_id: str):
            for w in self._workers:
                if w.id == worker_id:
                    w.status = WorkerStatus.KILLED
                    w.pid = None
                    return w
            raise KeyError(worker_id)

        def redirect(self, worker_id: str, target: str):
            raise NotImplementedError

        def restart(self, worker_id: str):
            raise NotImplementedError

    cli = Worker(
        id="gemini-cli-50200",
        name="Gemini CLI · 50200",
        status=WorkerStatus.RUNNING,
        pid=50200,
        source="cli",
        cmdline_short="/opt/homebrew/bin/gemini",
    )
    reg = Registry(RecordingAdapter([cli]))
    armed = reg.request_kill("gemini-cli-50200")
    assert armed["armed"] is True
    out = reg.execute_kill("gemini-cli-50200", "confirm kill")
    assert out["after"]["status"] == "killed"


def test_voice_aliases_for_cli():
    cmd = parse_command("pause grok cli")
    assert cmd and cmd.action == "pause" and cmd.worker_id == "grok-cli"
    cmd = parse_command("pause gemini")
    assert cmd and cmd.action == "pause" and cmd.worker_id == "gemini-cli"
    cmd = parse_command("inspect grok")
    assert cmd and cmd.action == "inspect" and cmd.worker_id == "grok-cli"
