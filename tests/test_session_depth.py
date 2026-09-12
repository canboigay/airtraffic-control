"""Tower session depth: idle/stale, left-off transcripts, steer-prompt."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from backend.adapters.god_mode import GodModeAdapter, Proc
from backend.commands import parse_command
from backend.registry import Registry, WorkerStatus
from backend.session_depth import (
    compute_activity_status,
    deliver_steer_prompt,
    find_claude_jsonl,
    inject_tty_prompt,
    read_claude_left_off,
    write_steer_inbox,
)

STACK = "/Users/simeong/local-claude-offline-stack"
SID = "146e90ce-078b-4f95-9200-1a4d52322c0c"


def _proc(pid, cmd, ppid=1, state="S", tty="ttys000", cpu=0.0):
    return Proc(pid=pid, ppid=ppid, state=state, command=cmd, user="simeong", tty=tty, cpu=cpu)


def test_compute_idle_and_stale():
    assert compute_activity_status(state="T", cpu=0, source="session") == "paused"
    assert compute_activity_status(state="S", cpu=0.1, source="session") == "idle"
    assert compute_activity_status(state="R", cpu=12.0, source="session") == "running"
    # demo never idles
    assert compute_activity_status(state="S", cpu=0.0, source="demo") == "running"
    now = time.time()
    assert (
        compute_activity_status(
            state="S",
            cpu=0.0,
            source="cli",
            transcript_mtime=now - 900,
            now=now,
            stale_sec=600,
        )
        == "stale"
    )
    assert (
        compute_activity_status(
            state="S",
            cpu=0.0,
            source="session",
            transcript_mtime=now - 60,
            now=now,
            stale_sec=600,
        )
        == "idle"
    )


def test_find_real_claude_jsonl():
    p = find_claude_jsonl(SID)
    assert p is not None
    assert p.name == f"{SID}.jsonl"
    assert p.is_file()


def test_read_claude_left_off_not_campaign():
    left = read_claude_left_off(SID, stack=STACK, lines=5)
    assert left.source_kind in {"claude_jsonl", "god_last_answer"}
    blob = "\n".join(left.lines).lower()
    assert "giantsequoia" not in blob
    assert left.lines, "expected transcript lines from real session"


def test_steer_inbox_only(tmp_path):
    path = write_steer_inbox(
        worker_id="claude-session-901",
        prompt="status check please",
        session_id=SID,
        tty="ttys000",
        inbox_root=tmp_path,
    )
    assert path.is_file()
    data = json.loads(path.read_text())
    assert data["prompt"] == "status check please"
    assert data["session_id"] == SID


def test_deliver_steer_inbox_and_mocked_tty(tmp_path):
    injected = {}

    def fake_inject(tty, prompt):
        injected["tty"] = tty
        injected["prompt"] = prompt

    out = deliver_steer_prompt(
        worker_id="claude-session-901",
        prompt="hello tower",
        session_id=SID,
        tty="ttys000",
        method="auto",
        inbox_root=tmp_path,
        inject_fn=fake_inject,
    )
    assert out.ok
    assert out.method == "inbox+tty"
    assert injected == {"tty": "ttys000", "prompt": "hello tower"}

    out2 = deliver_steer_prompt(
        worker_id="claude-session-901",
        prompt="inbox only",
        session_id=SID,
        tty="ttys000",
        method="inbox",
        inbox_root=tmp_path,
        inject_fn=fake_inject,
    )
    assert out2.ok and out2.method == "inbox"


def test_parse_steer_prompt_phrases():
    cmd = parse_command("tell claude session to summarize findings")
    assert cmd and cmd.action == "steer_prompt"
    assert cmd.worker_id == "claude-session"
    assert "summarize" in (cmd.target or "")

    cmd = parse_command("prompt god session with check campaign status")
    assert cmd and cmd.action == "steer_prompt" and cmd.worker_id == "god-session"

    # god-rt quiet verb stays redirect
    cmd = parse_command("steer god-rt to brief")
    assert cmd and cmd.action == "redirect" and cmd.worker_id == "god-rt"


def test_list_workers_idle_badge_and_child_session(tmp_path):
    terminal = _proc(704, "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal", tty="??")
    zsh = _proc(835, "-zsh", ppid=704)
    god = _proc(900, f"zsh {STACK}/god", ppid=835, state="S", cpu=0.0)
    claude = _proc(
        901,
        f"claude --resume {SID}",
        ppid=900,
        state="S",
        cpu=0.0,
    )
    adapter = GodModeAdapter(
        stack=STACK,
        include_demo=False,
        _procs=[terminal, zsh, god, claude],
        _deny_pids=set(),
        targets_path=tmp_path / "t.json",
    )
    by_id = {w.id: w for w in adapter.list_workers()}
    assert "claude-session-901" in by_id
    cw = by_id["claude-session-901"]
    assert cw.status in {WorkerStatus.IDLE, WorkerStatus.STALE}
    assert cw.session_id == SID
    assert cw.cpu_pct == 0.0

    gw = by_id["god-session-900"]
    assert gw.protected is True
    # god inherits child resume for transcript depth
    assert gw.session_id == SID
    assert "near resume" not in (gw.session_hint or "")
    assert "resume 146e90ce" in (gw.session_hint or "")


def test_inspect_session_uses_transcript_not_campaign(tmp_path, monkeypatch):
    stack = tmp_path / "stack"
    logs = stack / "logs"
    logs.mkdir(parents=True)
    poison = logs / "god-red-giantsequoiamultiverse.com-20260909T070914Z.log"
    poison.write_text("PROBE hit giantsequoia\n")

    # Build a tiny fake home with jsonl
    home = tmp_path / "home"
    proj = home / ".claude" / "projects" / "-test"
    proj.mkdir(parents=True)
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    jl = proj / f"{sid}.jsonl"
    rec = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "Left off at KYC oracle."}]},
    }
    jl.write_text(json.dumps(rec) + "\n")

    claude = _proc(901, f"claude --resume {sid}", state="S", cpu=0.0)
    ad = GodModeAdapter(
        stack=str(stack),
        include_demo=False,
        _procs=[claude],
        _deny_pids=set(),
        targets_path=tmp_path / "t.json",
    )
    monkeypatch.setenv("HOME", str(home))
    # find_claude_jsonl uses HOME
    out = ad.inspect_worker("claude-session-901", lines=5)
    assert out.get("log_path")
    assert "giantsequoia" not in json.dumps(out).lower()
    assert any("KYC" in ln or "Left off" in ln for ln in out.get("lines") or [])
    assert out.get("left_off", {}).get("source_kind") == "claude_jsonl"


def test_registry_steer_prompt_allows_protected(tmp_path):
    from backend.registry import Worker

    w = Worker(
        id="claude-session-901",
        name="Claude Session · 901",
        status=WorkerStatus.IDLE,
        pid=901,
        source="session",
        protected=True,
        session_id=SID,
        session_tty="ttys000",
        cmdline_short=f"claude --resume {SID}",
    )

    class A:
        def list_workers(self):
            return [w]

        def steer_prompt(self, worker_id, prompt, *, method="auto"):
            return deliver_steer_prompt(
                worker_id=worker_id,
                prompt=prompt,
                session_id=SID,
                tty="ttys000",
                method="inbox",
                inbox_root=tmp_path,
                inject_fn=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no")),
            ).as_dict()

        def pause(self, *a, **k):
            raise NotImplementedError

        def resume(self, *a, **k):
            raise NotImplementedError

        def kill(self, *a, **k):
            raise NotImplementedError

        def redirect(self, *a, **k):
            raise NotImplementedError

        def restart(self, *a, **k):
            raise NotImplementedError

    reg = Registry(A())
    out = reg.steer_prompt("claude-session-901", "ping", method="inbox")
    assert out["steer_prompt"]["ok"] is True
    assert out["steer_prompt"]["method"] == "inbox"


def test_inject_tty_prompt_uses_tiocsti_and_cr(monkeypatch, tmp_path):
    """Unanimous path: TIOCSTI keystrokes + CR Enter (agy/grok/god)."""
    import backend.session_depth as sd

    calls = []
    fake_dev = tmp_path / "ttys999"
    fake_dev.write_text("")

    def fake_open(path, flags):
        calls.append(("open", path, flags))
        return 77

    def fake_ioctl(fd, op, arg):
        calls.append(("ioctl", fd, op, arg))

    def fake_close(fd):
        calls.append(("close", fd))

    monkeypatch.setattr(sd.os, "open", fake_open)
    monkeypatch.setattr(sd.os, "close", fake_close)
    monkeypatch.setattr(sd.fcntl, "ioctl", fake_ioctl)
    monkeypatch.setattr(sd, "Path", lambda *a, **k: fake_dev if a and str(a[0]).startswith("/dev") else Path(*a))

    # Path("/dev") / t must resolve to fake — patch exists check via wrapping inject
    real_path = sd.Path

    class DevPath:
        def __init__(self, *parts):
            self._p = real_path(*parts) if parts else real_path()
        def __truediv__(self, other):
            if str(self._p) == "/dev" or str(self._p).endswith("/dev"):
                return fake_dev
            return DevPath(self._p / other)
        def exists(self):
            return True
        def __str__(self):
            return str(fake_dev)

    monkeypatch.setattr(sd, "Path", DevPath)

    inject_tty_prompt("ttys999", "hello agy")
    ioctls = [c for c in calls if c[0] == "ioctl"]
    chars = "".join(c[3] for c in ioctls)
    assert chars == "hello agy\r"
    assert chars.endswith("\r")
    assert "\n" not in chars
    assert any(c[0] == "close" for c in calls)


def test_deliver_steer_marks_cr_submit(tmp_path):
    seen = {}

    def fake_inject(tty, prompt):
        seen["tty"] = tty
        seen["prompt"] = prompt

    out = deliver_steer_prompt(
        worker_id="agy-cli-1",
        prompt="ping",
        tty="ttys001",
        method="auto",
        inbox_root=tmp_path,
        inject_fn=fake_inject,
    )
    assert out.ok and out.method == "inbox+tty"
    assert out.submit == "cr+tiocsti"
    assert "Enter" in out.summary
    assert seen == {"tty": "ttys001", "prompt": "ping"}
