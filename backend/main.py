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

from backend.adapters.composite import build_fleet
from backend.adapters.god_mode import GodModeAdapter
from backend.audit import audit_log
from backend.agent import run_tower_agent
from backend.session_memory import tower_memory
from backend.tower_voice import sanitize_for_tts, synthesize_mp3
from backend.registry import CONFIRM_PHRASE, Registry
from backend.speechmatics import mint_rt_jwt

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

_fleet = build_fleet()
ADAPTER_MODE = _fleet.mode
demo_adapter = _fleet.demo
god_adapter = _fleet.god
registry = Registry(_fleet.adapter)

# Pending kill target for confirm-without-name flow
_pending_kill_worker: str | None = None


def _set_pending(wid: str | None) -> None:
    global _pending_kill_worker
    _pending_kill_worker = wid


@asynccontextmanager
async def lifespan(app: FastAPI):
    if demo_adapter is not None:
        demo_adapter.start_all()
    audit_log.record(
        "system.start",
        detail={
            "adapter_mode": ADAPTER_MODE,
            "workers": [w["id"] for w in registry.list_workers()],
        },
        source="system",
    )
    yield
    if demo_adapter is not None:
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
    focus_worker_id: str | None = None


class KillConfirmRequest(BaseModel):
    worker_id: str
    confirm: str


class RedirectRequest(BaseModel):
    target: str


class SteerPromptRequest(BaseModel):
    prompt: str
    method: str = "auto"  # inbox | tty | auto


class TextCommandRequest(BaseModel):
    text: str
    source: str = "text"
    focus_worker_id: str | None = None


class FocusRequest(BaseModel):
    worker_id: str | None = None


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "airtraffic-control",
        "adapter_mode": ADAPTER_MODE,
        "adapter": ADAPTER_MODE,
    }


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


@app.post("/api/workers/{worker_id}/restart")
def restart_worker(worker_id: str) -> dict[str, Any]:
    try:
        result = registry.restart(worker_id)
    except (KeyError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    audit_log.record(
        "restart",
        worker_id=worker_id,
        before=result["before"],
        after=result["after"],
        source="api",
    )
    return result




@app.post("/api/workers/{worker_id}/steer")
def steer_worker(worker_id: str, body: SteerPromptRequest) -> dict[str, Any]:
    """Prompt an active session/CLI from the tower UI (same path as voice/text)."""
    try:
        result = registry.steer_prompt(worker_id, body.prompt, method=body.method or "auto")
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except (RuntimeError, PermissionError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    audit_log.record(
        "steer_prompt",
        worker_id=worker_id,
        detail={
            "method": (result.get("steer_prompt") or {}).get("method"),
            "summary": (result.get("steer_prompt") or {}).get("summary"),
        },
        before=result.get("before"),
        after=result.get("after"),
        source="api",
    )
    spoken = (result.get("steer_prompt") or {}).get("summary") or "Prompt queued."
    # UI/API steer is silent — frontend must not TTS unless voice path asked.
    return {
        **result,
        "spoken_reply": spoken,
        "action": "steer_prompt",
        "speak": False,
    }


@app.get("/api/workers/{worker_id}/inspect")
def inspect_worker(worker_id: str, lines: int = 20) -> dict[str, Any]:
    try:
        result = registry.inspect_worker(worker_id, lines=lines)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    audit_log.record(
        "inspect",
        worker_id=result.get("worker_id") or worker_id,
        detail={"lines": lines, "summary": result.get("summary")},
        source="api",
    )
    return result


@app.post("/api/fleet/pause_all")
def pause_all_workers() -> dict[str, Any]:
    result = registry.pause_all()
    audit_log.record(
        "pause_all",
        detail={"paused_count": result.get("paused_count")},
        source="api",
    )
    return result


@app.post("/api/fleet/resume_all")
def resume_all_workers() -> dict[str, Any]:
    result = registry.resume_all()
    audit_log.record(
        "resume_all",
        detail={"resumed_count": result.get("resumed_count")},
        source="api",
    )
    return result

@app.get("/api/audit")
def get_audit(limit: int = 50) -> dict[str, Any]:
    return {"entries": audit_log.list(limit=limit)}


@app.get("/api/adapters/god-mode")
def god_mode_info() -> dict[str, Any]:
    ga = god_adapter if god_adapter is not None else GodModeAdapter()
    info = ga.describe_interface()
    info["adapter_mode"] = ADAPTER_MODE
    return info




@app.get("/api/memory")
def get_memory() -> dict[str, Any]:
    """Recent tower dialogue (anaphora context)."""
    return tower_memory.snapshot()


@app.post("/api/memory/clear")
def clear_memory() -> dict[str, Any]:
    tower_memory.clear()
    return {"ok": True, "cleared": True}


@app.post("/api/focus")
def set_focus(body: FocusRequest) -> dict[str, Any]:
    """Set tower UI focus (slide-out worker panel) for voice/text anaphora."""
    tower_memory.set_focus(body.worker_id)
    return {"ok": True, "focus_worker_id": tower_memory.focus_worker_id()}


@app.delete("/api/focus")
def clear_focus() -> dict[str, Any]:
    tower_memory.clear_focus()
    return {"ok": True, "focus_worker_id": None}


@app.post("/api/command")
def voice_command(body: CommandRequest) -> dict[str, Any]:
    try:
        if body.focus_worker_id is not None:
            tower_memory.set_focus(body.focus_worker_id or None)
        return run_tower_agent(
            body.transcript,
            registry=registry,
            audit_log=audit_log,
            get_pending=lambda: _pending_kill_worker,
            set_pending=lambda wid: _set_pending(wid),
            confirm_phrase=CONFIRM_PHRASE,
            source=body.source,
            memory=tower_memory,
        )
    except (KeyError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/command/text")
def text_command(body: TextCommandRequest) -> dict[str, Any]:
    """Text or voice transcript → full tool-calling tower chatbot."""
    try:
        if body.focus_worker_id is not None:
            tower_memory.set_focus(body.focus_worker_id or None)
        return run_tower_agent(
            body.text,
            registry=registry,
            audit_log=audit_log,
            get_pending=lambda: _pending_kill_worker,
            set_pending=lambda wid: _set_pending(wid),
            confirm_phrase=CONFIRM_PHRASE,
            source=body.source,
            memory=tower_memory,
        )
    except (KeyError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


class TtsRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)


@app.post("/api/tts")
async def tts(body: TtsRequest):
    """Neural TTS (edge-tts) for tower talk-back."""
    from fastapi.responses import Response

    text = sanitize_for_tts(body.text)
    if not text:
        raise HTTPException(status_code=400, detail="empty text")
    try:
        audio = await synthesize_mp3(text)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"tts failed: {e}") from e
    return Response(content=audio, media_type="audio/mpeg")


# Static frontend
FRONTEND = ROOT / "frontend"
if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(
        FRONTEND / "index.html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


def run() -> None:
    import uvicorn

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8765"))
    uvicorn.run("backend.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    run()
