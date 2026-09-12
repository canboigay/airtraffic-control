"""Natural tower talk-back: reply text + optional edge-tts audio."""

from __future__ import annotations

import io
import json
import os
import re
from typing import Any

import httpx

OPENROUTER_URL = os.environ.get("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions")
OPENROUTER_MODEL = os.environ.get("ATC_OPENROUTER_MODEL", "deepseek/deepseek-chat")
DEEPSEEK_URL = os.environ.get("DEEPSEEK_URL", "https://api.deepseek.com/chat/completions")
DEEPSEEK_MODEL = os.environ.get("ATC_DEEPSEEK_MODEL", "deepseek-chat")
TTS_VOICE = os.environ.get("ATC_TTS_VOICE", "en-US-AvaNeural")


def _fallback_reply(heard: str, payload: dict[str, Any]) -> str:
    action = payload.get("action")
    ok = payload.get("ok", False)
    if not ok:
        err = payload.get("error") or "That did not go through."
        if action == "unknown":
            return (
                "I heard you, but I am not sure what to do with that. "
                "Try pause, resume, kill, or status."
            )
        return f"Unable. {err}"

    result = payload.get("result") or {}
    if action == "status":
        workers = result.get("workers") or []
        bits = []
        for w in workers:
            name = w.get("name")
            status = w.get("status")
            pid = w.get("pid")
            bit = f"{name} is {status}"
            if pid:
                bit += f", pid {pid}"
            bits.append(bit)
        if not bits:
            return "Fleet is empty."
        return "Status: " + "; ".join(bits) + "."

    if action == "kill.arm":
        before = result.get("before") or {}
        name = before.get("name") or result.get("worker_id") or "worker"
        pid = before.get("pid")
        msg = f"{name} armed for kill"
        if pid:
            msg += f", pid {pid}"
        return msg + ". Say confirm kill to proceed."

    if action in {"pause", "resume", "kill", "redirect"}:
        after = result.get("after") or {}
        before = result.get("before") or {}
        name = after.get("name") or before.get("name") or "Worker"
        if action == "kill":
            old = before.get("pid")
            return f"{name} terminated. Was pid {old}."
        if action == "pause":
            pid = after.get("pid")
            return f"{name} paused" + (f", pid {pid}" if pid else "") + "."
        if action == "resume":
            pid = after.get("pid")
            return f"{name} back online" + (f", pid {pid}" if pid else "") + "."
        if action == "redirect":
            steer = result.get("steer") or {}
            if steer.get("verb") and steer.get("ok"):
                summary = steer.get("summary") or steer.get("verb")
                short = summary if len(summary) <= 90 else summary[:87] + "..."
                return f"{name} steered to {steer.get('verb')}. {short}"
            if steer.get("error"):
                return f"{name} steer failed: {steer.get('error')}"
            target = after.get("target") or "new target"
            return f"{name} redirected to {target}."

    return "Done."


def _llm_reply(heard: str, payload: dict[str, Any], fallback: str) -> str:
    system = (
        "You are AiRTraffic Control, a calm tower controller for AI agent fleets. "
        "Speak ONE short radio-style reply (1-2 sentences, max 35 words). "
        "Natural, confident, no markdown, no emoji, no quotes. "
        "Acknowledge what the supervisor said and confirm the outcome with names/PIDs when useful."
    )
    user = (
        "Supervisor said: "
        + repr(heard)
        + "\nSystem result JSON: "
        + json.dumps(payload, default=str)[:1800]
        + "\nDraft fallback if needed: "
        + fallback
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    providers = []
    or_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if or_key:
        providers.append((OPENROUTER_URL, or_key, OPENROUTER_MODEL, True))
    ds_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if ds_key:
        providers.append((DEEPSEEK_URL, ds_key, DEEPSEEK_MODEL, False))

    for url, key, model, is_or in providers:
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        if is_or:
            headers["HTTP-Referer"] = "https://github.com/canboigay/airtraffic-control"
            headers["X-Title"] = "AiRTraffic Control"
        body = {"model": model, "temperature": 0.4, "messages": messages}
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(url, headers=headers, json=body)
                resp.raise_for_status()
                text = resp.json()["choices"][0]["message"]["content"].strip()
                text = text.strip("\"'")
                if text:
                    return text[:280]
        except Exception:
            continue
    return fallback


def craft_chat_reply(heard: str) -> str:
    """Open conversation when the utterance is not a fleet control action."""
    system = (
        "You are AiRTraffic Control — a sharp, calm tower controller for AI agent fleets. "
        "The supervisor may talk about anything: ideas, questions, jokes, plans, unrelated topics. "
        "Answer naturally and helpfully in 1-3 short sentences (max 60 words). "
        "Stay in character as tower, lightly. No markdown, no emoji, no quotes around the whole reply. "
        "If they might want fleet control later, you can briefly offer that, but do not force it."
    )
    user = "Supervisor said: " + repr(heard)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    providers = []
    or_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if or_key:
        providers.append((OPENROUTER_URL, or_key, OPENROUTER_MODEL, True))
    ds_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if ds_key:
        providers.append((DEEPSEEK_URL, ds_key, DEEPSEEK_MODEL, False))

    for url, key, model, is_or in providers:
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        if is_or:
            headers["HTTP-Referer"] = "https://github.com/canboigay/airtraffic-control"
            headers["X-Title"] = "AiRTraffic Control"
        body = {"model": model, "temperature": 0.7, "messages": messages}
        try:
            with httpx.Client(timeout=12.0) as client:
                resp = client.post(url, headers=headers, json=body)
                resp.raise_for_status()
                text = resp.json()["choices"][0]["message"]["content"].strip()
                if text.startswith((""", "'")) and text.endswith((""", "'")) and len(text) > 1:
                    text = text[1:-1].strip()
                if text:
                    return text[:400]
        except Exception:
            continue
    return (
        "Copy. I can talk about that, but my link is a bit thin right now. "
        "Ask again, or give me a fleet order like status or pause log spam."
    )


def craft_spoken_reply(heard: str, payload: dict[str, Any]) -> str:
    if payload.get("action") in {"unknown", "chat"} or (
        not payload.get("ok") and payload.get("action") == "unknown"
    ):
        return craft_chat_reply(heard or "")
    fb = _fallback_reply(heard, payload)
    try:
        return _llm_reply(heard, payload, fb)
    except Exception:
        return fb



def sanitize_for_tts(text: str, *, limit: int = 420) -> str:
    s = (text or "").strip()
    s = re.sub(r"```[\s\S]*?```", " ", s)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"\*([^*]+)\*", r"\1", s)
    s = re.sub(r"^\s*[-•]\s+", "", s, flags=re.M)
    s = re.sub(r"[\r\n]+", " ", s)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return (s or "Copy.")[:limit]

async def synthesize_mp3(text: str) -> bytes:
    text = sanitize_for_tts(text)
    import edge_tts

    communicate = edge_tts.Communicate(text, TTS_VOICE, rate="+5%")
    buf = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buf.write(chunk["data"])
    return buf.getvalue()
