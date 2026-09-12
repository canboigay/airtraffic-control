
"""Inspect must not attach unrelated stack campaign logs."""
from pathlib import Path
from backend.adapters.god_mode import GodModeAdapter, Proc

def test_inspect_skips_unrelated_stack_logs(tmp_path, monkeypatch):
    stack = tmp_path / "stack"
    logs = stack / "logs"
    logs.mkdir(parents=True)
    poison = logs / "god-red-giantsequoiamultiverse.com-20260909T070914Z.log"
    poison.write_text("PROBE hit giantsequoia\n")
    ad = GodModeAdapter(stack=str(stack), include_demo=False)
    proc = Proc(pid=64928, ppid=1, state="R", command="agy --dangerously-skip-permissions", user="simeong")
    monkeypatch.setattr(ad, "_get", lambda wid: ("agy-cli-64928", proc))
    # Isolate from live ~/.gemini antigravity brains
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir(parents=True, exist_ok=True)
    out = ad.inspect_worker("agy-cli-64928", lines=5)
    assert "giantsequoia" not in (out.get("summary") or "").lower()
    assert "giantsequoia" not in "\n".join(out.get("lines") or []).lower()
    # Without cwd/session mapping: honest empty — never campaign log
    assert out.get("note") in {"no log file", "no transcript yet", None} or out.get("left_off", {}).get("note") == "no transcript yet"
    if out.get("log_path"):
        assert "god-red" not in out["log_path"]
        assert "giantsequoia" not in out["log_path"]
