"""Product-depth: inspect phrases, pause_all/resume_all, redirect target file."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from backend.adapters.demo import DemoAdapter, LOGS_DIR
from backend.commands import parse_command
from backend.registry import Registry, read_target_file, write_target_file


@pytest.mark.parametrize(
    "phrase,action,worker_id",
    [
        ("what's fake build doing", "inspect", "fake-build"),
        ("whats fake build doing", "inspect", "fake-build"),
        ("inspect log spam", "inspect", "log-spam"),
        ("status of research logs", "inspect", "fake-research"),
        ("tail build", "inspect", "fake-build"),
        ("pause all", "pause_all", None),
        ("hold the fleet", "pause_all", None),
        ("resume all", "resume_all", None),
        ("wake everyone", "resume_all", None),
    ],
)
def test_parse_depth_phrases(phrase, action, worker_id):
    cmd = parse_command(phrase)
    assert cmd is not None
    assert cmd.action == action
    assert cmd.worker_id == worker_id


def test_parse_single_pause_still_works():
    cmd = parse_command("pause log spam")
    assert cmd and cmd.action == "pause" and cmd.worker_id == "log-spam"


def test_parse_pause_all_defaults_demo_scope():
    cmd = parse_command("pause all")
    assert cmd and cmd.action == "pause_all" and cmd.scope == "demo"
    cmd = parse_command("resume all")
    assert cmd and cmd.action == "resume_all" and cmd.scope == "demo"


@pytest.fixture()
def registry():
    adapter = DemoAdapter()
    adapter.start_all()
    reg = Registry(adapter)
    time.sleep(0.4)
    yield reg
    adapter.shutdown_all()


def test_pause_all_and_resume_all(registry: Registry):
    workers = registry.list_workers()
    assert len(workers) == 3
    # pause one first — pause_all should skip it
    registry.pause("log-spam")
    result = registry.pause_all()
    assert result["paused_count"] == 2
    assert result["skipped_count"] >= 1
    statuses = {w["id"]: w["status"] for w in registry.list_workers()}
    assert statuses["log-spam"] == "paused"
    assert statuses["fake-build"] == "paused"
    assert statuses["fake-research"] == "paused"

    resumed = registry.resume_all()
    assert resumed["resumed_count"] == 3
    for w in registry.list_workers():
        assert w["status"] == "running"


def test_redirect_writes_target_file(registry: Registry, tmp_path: Path):
    # Use real logs dir (workers read from there)
    result = registry.redirect("fake-build", "docs")
    assert result["after"]["target"] == "docs"
    target_path = LOGS_DIR / "fake-build.target"
    assert target_path.exists()
    assert read_target_file("fake-build", LOGS_DIR) == "docs"


def test_redirect_appears_in_worker_log(registry: Registry):
    registry.redirect("fake-build", "docs")
    # Wait for worker loop to pick up target and log
    deadline = time.time() + 8
    found = False
    log_path = LOGS_DIR / "fake-build.log"
    while time.time() < deadline:
        if log_path.exists():
            text = log_path.read_text(encoding="utf-8", errors="replace")
            if "target=docs" in text or "docs" in text.split("fake-build")[-1][-500:]:
                # Prefer explicit target=docs from updated worker
                if "target=docs" in text:
                    found = True
                    break
        time.sleep(0.5)
    assert found, "expected target=docs in fake-build.log after redirect"


def test_inspect_worker_demo(registry: Registry):
    # ensure some log lines
    time.sleep(2.5)
    result = registry.inspect_worker("log-spam", lines=10)
    assert result["worker_id"] == "log-spam"
    assert result["source"] == "demo"
    assert result["line_count"] >= 1
    assert result["lines"]
    assert "summary" in result


def test_worker_snapshot_has_rich_fields(registry: Registry):
    w = registry.get("fake-research")
    assert w is not None
    assert w.get("source") == "demo"
    assert w.get("cmdline_short")
    assert w.get("uptime_sec") is not None
    assert w.get("detail")


def test_write_target_helper(tmp_path: Path):
    p = write_target_file("x", "alpha", tmp_path)
    assert p.read_text().strip() == "alpha"
    assert read_target_file("x", tmp_path) == "alpha"
