"""UI focus (slide-out panel) scopes voice/text anaphora to the selected worker."""

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
                "source": "demo",
            },
            {
                "id": "log-spam",
                "name": "Log Spam",
                "status": "running",
                "pid": 101,
                "target": None,
                "source": "demo",
            },
        ]

    def list_workers(self):
        return list(self.workers)

    def resolve_id(self, name):
        key = (name or "").strip().lower().replace(" ", "-")
        for w in self.workers:
            if w["id"] == key or w["name"].lower().replace(" ", "-") == key:
                return w["id"]
        return key if any(w["id"] == key for w in self.workers) else None

    def pause(self, wid):
        self.paused.append(wid)
        for w in self.workers:
            if w["id"] == wid:
                before = dict(w)
                w["status"] = "paused"
                return {"before": before, "after": dict(w)}
        raise KeyError(wid)

    def inspect_worker(self, wid, lines=20):
        return {
            "ok": True,
            "worker_id": wid,
            "summary": f"{wid} is compiling fixtures.",
            "lines": [f"[{wid}] tick"],
            "left_off": {"lines": [f"[{wid}] left off here"], "source_kind": "log"},
        }

    def status_summary(self):
        return {"count": len(self.workers), "workers": self.list_workers(), "by_status": {}}


class _Audit:
    def record(self, *a, **k):
        return None


def test_focus_prefers_over_history():
    mem = SessionMemory()
    mem.record(
        "pause log spam",
        spoken_reply="paused",
        action="pause",
        worker_id="log-spam",
        ok=True,
    )
    assert mem.last_worker_id() == "log-spam"
    mem.set_focus("fake-build")
    assert mem.focus_worker_id() == "fake-build"
    assert mem.last_worker_id() == "fake-build"
    snap = mem.snapshot()
    assert snap["focus_worker_id"] == "fake-build"
    assert snap["last_worker_id"] == "fake-build"
    assert "focus" in mem.prompt_block().lower() or "UI focus" in mem.prompt_block()


def test_apply_memory_uses_focus_worker():
    cmd = parse_command("pause it")
    filled = apply_memory(cmd, last_worker_id="fake-build")
    assert filled.worker_id == "fake-build"


def test_parse_steer_to_without_named_worker():
    cmd = parse_command("steer to brief status")
    assert cmd and cmd.from_memory and cmd.memory_ref == "it"
    assert cmd.target and "brief" in cmd.target


def test_fast_path_pause_it_uses_focus():
    mem = SessionMemory()
    # History points at log-spam, but panel focus is fake-build
    mem.record(
        "status of log spam",
        spoken_reply="ok",
        action="inspect",
        worker_id="log-spam",
        ok=True,
    )
    mem.set_focus("fake-build")
    reg = _FakeReg()
    out = _fast_fleet_action(
        "pause it",
        registry=reg,
        audit_log=_Audit(),
        get_pending=lambda: None,
        set_pending=lambda w: None,
        confirm_phrase="confirm kill",
        source="voice",
        fleet=reg.list_workers(),
        memory=mem,
    )
    assert out is not None and out["ok"]
    assert reg.paused == ["fake-build"]
    assert out.get("worker_id") == "fake-build"


def test_fast_path_whats_it_doing_uses_focus():
    mem = SessionMemory()
    mem.set_focus("fake-build")
    reg = _FakeReg()
    out = _fast_fleet_action(
        "what's it doing",
        registry=reg,
        audit_log=_Audit(),
        get_pending=lambda: None,
        set_pending=lambda w: None,
        confirm_phrase="confirm kill",
        source="voice",
        fleet=reg.list_workers(),
        memory=mem,
    )
    assert out is not None
    assert out.get("action") == "inspect"
    assert out.get("worker_id") == "fake-build"


def test_clear_focus_falls_back_to_history():
    mem = SessionMemory()
    mem.record(
        "pause fake build",
        action="pause",
        worker_id="fake-build",
        ok=True,
    )
    mem.set_focus("log-spam")
    mem.clear_focus()
    assert mem.focus_worker_id() is None
    assert mem.last_worker_id() == "fake-build"


def test_run_tower_agent_with_focus():
    mem = SessionMemory()
    mem.set_focus("log-spam")
    reg = _FakeReg()
    out = run_tower_agent(
        "pause it",
        registry=reg,
        audit_log=_Audit(),
        get_pending=lambda: None,
        set_pending=lambda w: None,
        confirm_phrase="confirm kill",
        source="text",
        memory=mem,
    )
    assert out["ok"]
    assert "log-spam" in reg.paused
