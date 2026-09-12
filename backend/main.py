"""AiRTraffic Control — FastAPI backend."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.adapters.demo import DemoAdapter
from backend.adapters.god_mode import GodModeAdapter
from backend.audit import audit_log
from backend.commands import command_to_dict, parse_command
from backend.registry import CONFIRM_PHRASE, Registry
from backend.speechmatics import mint_rt_jwt

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

demo_adapter = DemoAdapter()
registry = Registry(demo_adapter)

# Pending kill target for confirm-without-name flow
_pending_kill_worker: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    demo_adapter.start_all()
    audit_log.record("system.start", detail={"workers": [w["id"] for w in registry.list_workers()]}, source="system")
    yield
    demo_adapter.shutdown_all()
    audit_log.record("system.stop", source="system")


app = FastAPI(title="AiRTraffic Control", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class TokenRequest(BaseModel):
    ttl_seconds: int = Field(default=3600, ge=60, le=86400)


class CommandRequest(BaseModel):
    transcript: str
    source: str = "voice"


class KillConfirmRequest(BaseModel):
    worker_id: str
    confirm: str


class RedirectRequest(BaseModel):
    target: str


class TextCommandRequest(BaseModel):
    text: str
    source: str = "text"


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "airtraffic-control"}


@app.post("/api/speechmatics/token")
async def speechmatics_token(body: TokenRequest | None = None) -> dict[str, Any]:
    ttl = body.ttl_seconds if body else 3600
    try:
        return await mint_rt_jwt(ttl_seconds=ttl)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.get("/api/workers")
def list_workers() -> dict[str, Any]:
    return {"workers": registry.list_workers()}


@app.get("/api/status")
def status() -> dict[str, Any]:
    return registry.status_summary()


@app.post("/api/workers/{worker_id}/pause")
def pause_worker(worker_id: str) -> dict[str, Any]:
    try:
        result = registry.pause(worker_id)
    except (KeyError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    audit_log.record(
        "pause",
        worker_id=worker_id,
        before=result["before"],
        after=result["after"],
        source="api",
    )
    return result


@app.post("/api/workers/{worker_id}/resume")
def resume_worker(worker_id: str) -> dict[str, Any]:
    try:
        result = registry.resume(worker_id)
    except (KeyError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    audit_log.record(
        "resume",
        worker_id=worker_id,
        before=result["before"],
        after=result["after"],
        source="api",
    )
    return result


@app.post("/api/workers/{worker_id}/kill")
def arm_kill(worker_id: str) -> dict[str, Any]:
    global _pending_kill_worker
    try:
        result = registry.request_kill(worker_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    _pending_kill_worker = result["worker_id"]
    audit_log.record(
        "kill.arm",
        worker_id=result["worker_id"],
        before=result["before"],
        detail={"confirm_phrase": CONFIRM_PHRASE},
        source="api",
    )
    return result


@app.post("/api/workers/{worker_id}/kill/confirm")
def confirm_kill(worker_id: str, body: KillConfirmRequest) -> dict[str, Any]:
    global _pending_kill_worker
    try:
        result = registry.execute_kill(worker_id, body.confirm)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except PermissionError as e:
        audit_log.record(
            "kill.denied",
            worker_id=worker_id,
            detail={"reason": str(e), "confirm": body.confirm},
            source="api",
        )
        raise HTTPException(status_code=403, detail=str(e)) from e
    _pending_kill_worker = None
    audit_log.record(
        "kill",
        worker_id=worker_id,
        before=result["before"],
        after=result["after"],
        detail={"confirmed": True},
        source="api",
    )
    return result


@app.post("/api/workers/{worker_id}/redirect")
def redirect_worker(worker_id: str, body: RedirectRequest) -> dict[str, Any]:
    try:
        result = registry.redirect(worker_id, body.target)
    except (KeyError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    audit_log.record(
        "redirect",
        worker_id=worker_id,
        before=result["before"],
        after=result["after"],
        detail={"target": body.target},
        source="api",
    )
    return result


@app.get("/api/audit")
def get_audit(limit: int = 50) -> dict[str, Any]:
    return {"entries": audit_log.list(limit=limit)}


@app.get("/api/adapters/god-mode")
def god_mode_stub() -> dict[str, Any]:
    return GodModeAdapter().describe_interface()


def _execute_parsed(cmd, source: str) -> dict[str, Any]:
    global _pending_kill_worker
    action = cmd.action

    if action == "status":
        summary = registry.status_summary()
        audit_log.record("status", detail=summary, source=source)
        return {"ok": True, "action": "status", "result": summary}

    if action == "confirm_kill":
        wid = _pending_kill_worker
        if not wid:
            return {
                "ok": False,
                "action": "confirm_kill",
                "error": "No kill armed. Say kill <worker> first.",
            }
        try:
            result = registry.execute_kill(wid, CONFIRM_PHRASE)
        except PermissionError as e:
            audit_log.record("kill.denied", worker_id=wid, detail={"reason": str(e)}, source=source)
            return {"ok": False, "action": "confirm_kill", "error": str(e)}
        _pending_kill_worker = None
        audit_log.record(
            "kill",
            worker_id=wid,
            before=result["before"],
            after=result["after"],
            detail={"confirmed": True, "transcript": cmd.raw},
            source=source,
        )
        return {"ok": True, "action": "kill", "result": result}

    if action == "kill":
        result = registry.request_kill(cmd.worker_id)  # type: ignore[arg-type]
        _pending_kill_worker = result["worker_id"]
        audit_log.record(
            "kill.arm",
            worker_id=result["worker_id"],
            before=result["before"],
            detail={"confirm_phrase": CONFIRM_PHRASE, "transcript": cmd.raw},
            source=source,
        )
        return {"ok": True, "action": "kill.arm", "result": result}

    if action == "pause":
        result = registry.pause(cmd.worker_id)  # type: ignore[arg-type]
        audit_log.record(
            "pause",
            worker_id=cmd.worker_id,
            before=result["before"],
            after=result["after"],
            detail={"transcript": cmd.raw},
            source=source,
        )
        return {"ok": True, "action": "pause", "result": result}

    if action == "resume":
        result = registry.resume(cmd.worker_id)  # type: ignore[arg-type]
        audit_log.record(
            "resume",
            worker_id=cmd.worker_id,
            before=result["before"],
            after=result["after"],
            detail={"transcript": cmd.raw},
            source=source,
        )
        return {"ok": True, "action": "resume", "result": result}

    if action == "redirect":
        result = registry.redirect(cmd.worker_id, cmd.target or "")  # type: ignore[arg-type]
        audit_log.record(
            "redirect",
            worker_id=cmd.worker_id,
            before=result["before"],
            after=result["after"],
            detail={"target": cmd.target, "transcript": cmd.raw},
            source=source,
        )
        return {"ok": True, "action": "redirect", "result": result}

    return {"ok": False, "action": "unknown", "error": f"Could not parse: {cmd.raw}", "parsed": command_to_dict(cmd)}


@app.post("/api/command")
def voice_command(body: CommandRequest) -> dict[str, Any]:
    cmd = parse_command(body.transcript)
    if cmd is None:
        raise HTTPException(status_code=400, detail="empty transcript")
    try:
        return _execute_parsed(cmd, body.source)
    except (KeyError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/command/text")
def text_command(body: TextCommandRequest) -> dict[str, Any]:
    """Optional text fallback for demos without mic."""
    cmd = parse_command(body.text)
    if cmd is None:
        raise HTTPException(status_code=400, detail="empty text")
    try:
        return _execute_parsed(cmd, body.source)
    except (KeyError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# Static frontend
FRONTEND = ROOT / "frontend"
if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")


def run() -> None:
    import uvicorn

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8765"))
    uvicorn.run("backend.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    run()
