"""Rolling dialogue memory + anaphora fast path."""

from __future__ import annotations

from backend.agent import _fast_fleet_action, run_tower_agent
from backend.commands import apply_memory, parse_command
from backend.session_memory import SessionMemory


class _FakeReg:
    def __init__(self):
        self.paused = []
        self.workers = [
            {
                "id": "fake-build",
                "name": "Fake Build",
                "status": "running",
                "pid": 100,
                "target": None,
            }
        ]

    def list_workers(self):
        return list(self.workers)

    def resolve_id(self, name):
        key = name.strip().lower().replace(" ", "-")
        for w in self.workers:
            if w["id"] == key or w["name"].lower().replace(" ", "-") == key:
                return w["id"]
        if key.startswith("fake-build"):
            return "fake-build"
        return None

    def pause(self, wid):
        self.paused.append(wid)
        before = dict(self.workers[0])
        self.workers[0]["status"] = "paused"
        return {"before": before, "after": dict(self.workers[0])}

    def status_summary(self):
        return {"count": 1, "workers": self.list_workers(), "by_status": {}}


class _Audit:
    def record(self, *a, **k):
        return None


def test_parse_anaphora_and_again():
    pause_it = parse_command("pause it")
    assert pause_it and pause_it.action == "pause" and pause_it.from_memory
    again = parse_command("do that again")
    assert again and again.action == "repeat_last"
    clarify = parse_command("what did you mean by that")
    assert clarify and clarify.action == "clarify"


def test_parse_steer_god_rt():
    cmd = parse_command("steer god-rt to brief")
    assert cmd and cmd.action == "redirect"
    assert cmd.worker_id == "god-rt"
    assert cmd.target == "brief"
    cmd2 = parse_command("ask god rt for campaign status")
    assert cmd2 and cmd2.action == "redirect" and cmd2.worker_id == "god-rt"
    assert "campaign" in (cmd2.target or "")


def test_apply_memory_fills_worker():
    cmd = parse_command("pause it")
    filled = apply_memory(cmd, last_worker_id="fake-build")
    assert filled.worker_id == "fake-build"


def test_apply_memory_repeat():
    cmd = parse_command("do that again")
    filled = apply_memory(
        cmd,
        last_worker_id="fake-build",
        last_turn={"action": "pause", "worker_id": "fake-build", "ok": True},
    )
    assert filled.action == "pause"
    assert filled.worker_id == "fake-build"


def test_fast_path_pause_it_uses_memory():
    mem = SessionMemory()
    mem.record(
        "pause fake build",
        spoken_reply="Fake Build paused, pid 100.",
        action="pause",
        worker_id="fake-build",
        ok=True,
        source="text",
    )
    reg = _FakeReg()
    out = _fast_fleet_action(
        "pause it",
        registry=reg,
        audit_log=_Audit(),
        get_pending=lambda: None,
        set_pending=lambda w: None,
        confirm_phrase="confirm kill",
        source="text",
        fleet=reg.list_workers(),
        memory=mem,
    )
    assert out is not None
    assert out["ok"]
    assert out.get("from_memory")
    assert reg.paused == ["fake-build"]


def test_fast_path_clarify_recalls_reply():
    mem = SessionMemory()
    mem.record(
        "pause fake build",
        spoken_reply="Fake Build paused, pid 100.",
        action="pause",
        worker_id="fake-build",
        ok=True,
    )
    reg = _FakeReg()
    out = _fast_fleet_action(
        "what did you mean by that",
        registry=reg,
        audit_log=_Audit(),
        get_pending=lambda: None,
        set_pending=lambda w: None,
        confirm_phrase="confirm kill",
        source="text",
        fleet=reg.list_workers(),
        memory=mem,
    )
    assert out and out["action"] == "clarify"
    assert "Fake Build paused" in out["spoken_reply"]


def test_run_tower_agent_records_memory():
    mem = SessionMemory()
    reg = _FakeReg()
    out = run_tower_agent(
        "pause fake build",
        registry=reg,
        audit_log=_Audit(),
        get_pending=lambda: None,
        set_pending=lambda w: None,
        confirm_phrase="confirm kill",
        source="text",
        memory=mem,
    )
    assert out["ok"]
    assert mem.last_worker_id() == "fake-build"
    assert mem.last_action() == "pause"

    out2 = run_tower_agent(
        "pause it",
        registry=reg,
        audit_log=_Audit(),
        get_pending=lambda: None,
        set_pending=lambda w: None,
        confirm_phrase="confirm kill",
        source="text",
        memory=mem,
    )
    assert out2["ok"]
    assert out2.get("from_memory") or out2.get("fast_path")
