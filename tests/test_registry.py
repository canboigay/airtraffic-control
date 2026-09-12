"""Registry + confirm-gate tests (no live Speechmatics)."""

from __future__ import annotations

import time

import pytest

from backend.adapters.demo import DemoAdapter
from backend.registry import CONFIRM_PHRASE, Registry


@pytest.fixture()
def registry():
    adapter = DemoAdapter()
    adapter.start_all()
    reg = Registry(adapter)
    # give processes a moment to start
    time.sleep(0.3)
    yield reg
    adapter.shutdown_all()


def test_lists_three_workers_with_pids(registry: Registry):
    workers = registry.list_workers()
    assert len(workers) == 3
    ids = {w["id"] for w in workers}
    assert ids == {"log-spam", "fake-build", "fake-research"}
    for w in workers:
        assert w["status"] == "running"
        assert isinstance(w["pid"], int) and w["pid"] > 0


def test_pause_and_resume(registry: Registry):
    before = registry.get("fake-build")
    assert before and before["status"] == "running"
    result = registry.pause("fake-build")
    assert result["after"]["status"] == "paused"
    assert result["after"]["pid"] == before["pid"]
    result = registry.resume("fake-build")
    assert result["after"]["status"] == "running"


def test_kill_requires_confirm_phrase(registry: Registry):
    armed = registry.request_kill("log-spam")
    assert armed["armed"] is True
    assert armed["confirm_phrase"] == CONFIRM_PHRASE
    old_pid = armed["before"]["pid"]
    assert old_pid

    with pytest.raises(PermissionError):
        registry.execute_kill("log-spam", "yes do it")

    # still armed after bad confirm? — our impl leaves pending on failure
    result = registry.execute_kill("log-spam", CONFIRM_PHRASE)
    assert result["after"]["status"] == "killed"
    assert result["after"]["pid"] is None
    assert result["before"]["pid"] == old_pid


def test_kill_not_armed_denied(registry: Registry):
    with pytest.raises(PermissionError, match="not armed"):
        registry.execute_kill("fake-research", CONFIRM_PHRASE)


def test_redirect_sets_target(registry: Registry):
    result = registry.redirect("fake-research", "docs backlog")
    assert result["after"]["target"] == "docs backlog"
    assert result["after"]["pid"]  # still alive
