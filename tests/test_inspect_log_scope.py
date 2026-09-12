
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
    out = ad.inspect_worker("agy-cli-64928", lines=5)
    assert out.get("log_path") is None
    assert out.get("note") == "no log file"
    assert "giantsequoia" not in (out.get("summary") or "").lower()
