"""left_off_preview on list cards + LeftOff.preview helper."""

from __future__ import annotations

from backend.session_depth import LeftOff


def test_left_off_preview_from_last_line():
    left = LeftOff(lines=["older", "assistant: hello world"])
    assert left.preview() == "assistant: hello world"


def test_left_off_preview_truncates():
    left = LeftOff(lines=["x" * 200])
    out = left.preview(40)
    assert out is not None
    assert len(out) == 40
    assert out.endswith("…")


def test_left_off_preview_none_when_empty():
    assert LeftOff().preview() is None
    assert LeftOff(note="no transcript yet").preview() is None


def test_left_off_preview_uses_useful_note():
    left = LeftOff(note="god-last-answer: deepseek @ 2026-09-12")
    assert "deepseek" in (left.preview() or "")


def test_cancel_kill_disarms(monkeypatch, tmp_path):
    from backend.adapters.demo import DemoAdapter
    from backend.registry import Registry

    # Use demo adapter only — arm then cancel without killing
    demo = DemoAdapter()
    # Don't start demos; inject a fake worker via monkeypatch if needed
    reg = Registry(demo)
    workers = reg.list_workers()
    if not workers:
        # empty ok — invent pending directly
        reg._pending_kills["fake-build"] = "armed"
        out = reg.cancel_kill("fake-build")
        assert out["cancelled"] is True
        assert "fake-build" not in reg._pending_kills
        out2 = reg.cancel_kill("fake-build")
        assert out2["cancelled"] is False
        return
    wid = workers[0]["id"]
    armed = reg.request_kill(wid)
    assert armed["armed"] is True
    out = reg.cancel_kill(wid)
    assert out["cancelled"] is True
    # confirm should now fail (not armed)
    import pytest

    with pytest.raises(PermissionError, match="not armed"):
        reg.execute_kill(wid, "confirm kill")
