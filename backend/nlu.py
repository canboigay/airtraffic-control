"""Open-ended intent for AiRTraffic.

Ears = Speechmatics (frontend). Brain = LLM (OpenRouter → DeepSeek → Ollama) with rule fallback.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

from backend.commands import ParsedCommand, parse_command as parse_rules

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("ATC_NLU_MODEL", "llama3.2:1b")
OPENROUTER_URL = os.environ.get("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions")
OPENROUTER_MODEL = os.environ.get("ATC_OPENROUTER_MODEL", "deepseek/deepseek-chat")
DEEPSEEK_URL = os.environ.get("DEEPSEEK_URL", "https://api.deepseek.com/chat/completions")
DEEPSEEK_MODEL = os.environ.get("ATC_DEEPSEEK_MODEL", "deepseek-chat")

SYSTEM = """You map voice tower utterances to ONE JSON object. No markdown.

Allowed actions only:
status | pause | resume | kill | confirm_kill | redirect | clarify | pause_all | resume_all | session | unknown

Workers: use any worker id/name the supervisor says (demo: log-spam, fake-build, fake-research;
discovered: mcp-hands-<pid>, god-rt-<pid>, etc.). Prefer the exact id when known.

Schema:
{"action":"status|pause|resume|kill|confirm_kill|redirect|pause_all|resume_all|session|unknown","worker_id":null|"string","target":null|"string"}

Understand any phrasing, slang, or indirect ask.
confirm/yes/yep/do it/go ahead/affirmative → confirm_kill
fleet health / what's running / sitrep → status
pause/hold/freeze → pause (needs worker)
pause all / hold the fleet → pause_all of DEMO workers only (never live god/claude sessions)
pause god / pause god-rt → pause that God workload (explicit)
resume/continue/unpause → resume (needs worker)
kill/terminate/murder/shut down → kill (needs worker)
redirect/send/point/steer … to/at → redirect (needs worker + target; God RT quiet verbs: campaign_status, brief, list, ready, next, probe)
what did you mean / clarify → clarify
which terminal / what session / where is X → session (needs worker)
If the ask is outside this control surface (e.g. make coffee) → unknown with null worker_id.
"""


def _extract_json(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


def _from_dict(data: dict[str, Any], raw: str) -> ParsedCommand:
    action = str(data.get("action") or "unknown").strip().lower()
    if action not in {"status", "pause", "resume", "kill", "confirm_kill", "redirect", "clarify", "pause_all", "resume_all", "session", "unknown"}:
        action = "unknown"
    wid = data.get("worker_id")
    if wid is not None:
        wid = str(wid).strip()
        wid = wid.lower().replace("_", "-").replace(" ", "-") if wid else None
        if not wid:
            wid = None
    if action == "unknown":
        wid = None
    if action in {"pause", "resume", "kill", "redirect"} and not wid:
        # keep action but leave worker unresolved for caller
        pass
    target = data.get("target")
    if target is not None:
        target = str(target).strip() or None
    if action != "redirect":
        target = None
    return ParsedCommand(
        action=action,
        worker_id=wid,
        target=target,
        raw=raw,
        confirm_text="confirm kill" if action == "confirm_kill" else None,
    )


def _chat_openai_compat(url: str, api_key: str, model: str, transcript: str, timeout: float) -> ParsedCommand | None:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if "openrouter.ai" in url:
        headers["HTTP-Referer"] = "https://github.com/canboigay/airtraffic-control"
        headers["X-Title"] = "AiRTraffic Control"
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": transcript.strip()},
        ],
        "response_format": {"type": "json_object"},
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                # retry without response_format for providers that reject it
                payload.pop("response_format", None)
                resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
    except Exception:
        return None
    data = _extract_json(content)
    if not data:
        return None
    return _from_dict(data, transcript)


def parse_with_openrouter(transcript: str, timeout: float = 12.0) -> ParsedCommand | None:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        return None
    return _chat_openai_compat(OPENROUTER_URL, key, OPENROUTER_MODEL, transcript, timeout)


def parse_with_deepseek(transcript: str, timeout: float = 12.0) -> ParsedCommand | None:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        return None
    return _chat_openai_compat(DEEPSEEK_URL, key, DEEPSEEK_MODEL, transcript, timeout)


def parse_with_ollama(transcript: str, timeout: float = 8.0) -> ParsedCommand | None:
    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0, "num_predict": 120},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": transcript.strip()},
        ],
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(f"{OLLAMA_URL}/api/chat", json=payload)
            resp.raise_for_status()
            content = resp.json().get("message", {}).get("content", "")
    except Exception:
        return None
    data = _extract_json(content)
    if not data:
        return None
    return _from_dict(data, transcript)


def parse_with_llm(transcript: str) -> ParsedCommand | None:
    for fn in (parse_with_openrouter, parse_with_deepseek, parse_with_ollama):
        got = fn(transcript)
        if got is not None:
            return got
    return None


def parse_utterance(transcript: str) -> ParsedCommand | None:
    """Fast rules first; LLM brain for everything else."""
    ruled = parse_rules(transcript)
    if ruled is None:
        return None
    if ruled.action != "unknown":
        return ruled
    llm = parse_with_llm(transcript)
    return llm if llm is not None else ruled
