"""Full tool-calling tower chatbot.

Speechmatics (or text) supplies the utterance. This agent can:
- chat about anything
- call registry tools to actually control the demo fleet
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

import httpx

from backend.commands import apply_memory, parse_command

OPENROUTER_URL = os.environ.get("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions")
OPENROUTER_MODEL = os.environ.get("ATC_OPENROUTER_MODEL", "deepseek/deepseek-chat")
DEEPSEEK_URL = os.environ.get("DEEPSEEK_URL", "https://api.deepseek.com/chat/completions")
DEEPSEEK_MODEL = os.environ.get("ATC_DEEPSEEK_MODEL", "deepseek-chat")

SYSTEM_BASE = """You are AiRTraffic Control — a full conversational tower chatbot for an AI agent fleet.

You can talk about ANY topic naturally (ideas, jokes, weather, strategy, coding, life).
When the supervisor wants fleet control, use tools to DO it — don't just describe it.

Use the worker ids from the current fleet list below (ids, names, status, pid).
Accept common aliases (log spam, fake build, mcp hands, god-rt, …) and pass the matching id.

God Mode steer: redirect_worker on a God RT worker with quiet verbs
(campaign_status, brief, list, ready, next, probe) actually calls god-rt.
Do NOT request engage, exploit, GO, or loud promote from ATC.

Anaphora: use Recent dialogue below. "pause it" / "do that again" / "what did you mean"
refer to the last worker/action/reply. Prefer the remembered worker_id when pronouns appear.

Kill safety: call kill_worker first (arms), then confirm_kill only after they clearly confirm.
If the system prompt says a kill is already armed, and they say confirm/yes/do it, call confirm_kill immediately — do not ask whether one is armed.
Spoken replies MUST be plain speakable English for TTS:
- Prefer ONE sentence. Never more than two. Hard cap 22 words.
- For status: say count + only paused/killed names, not every PID.
- Radio-tower snappy. No fluff, no lists.
- no markdown, bullets, asterisks, code fences, or emoji
- no newlines; one continuous paragraph
After a single action: "{Name} {paused|resumed|killed}, pid {N}."
Fleet-wide: pause_all / resume_all default to DEMO workers only.
Never fleet-pause live god/claude session wrappers. Say "pause god-rt" or
"pause all god" only when the supervisor explicitly wants God workloads.
Kill of god/claude TUI wrappers is refused; demo and mcp-hands still need confirm kill.
Inspect: inspect_worker for log tails (summarize, don't dump).
Session/where: worker_session for which terminal / what session / where is X running (tty, Terminal.app/iTerm/Claude.app/god launcher, Claude --resume id).
Steer: "{Name} steered to {verb}. {short summary}."
"""


def _fleet_block(workers: list[dict[str, Any]]) -> str:
    if not workers:
        return "Current fleet:\n- (no workers discovered)"
    lines = ["Current fleet:"]
    for w in workers:
        pid = w.get("pid") if w.get("pid") is not None else "—"
        tgt = w.get("target") or "—"
        sess = w.get("session_hint") or ""
        extra = f" session={sess}" if sess else ""
        lines.append(
            f"- {w.get('id')} — {w.get('name')} status={w.get('status')} pid={pid} target={tgt}{extra}"
        )
    return "\n".join(lines)


def _system_prompt(
    workers: list[dict[str, Any]],
    pending_kill: str | None = None,
    memory_block: str | None = None,
) -> str:
    parts = [SYSTEM_BASE, _fleet_block(workers)]
    if memory_block:
        parts.append(memory_block)
    if pending_kill:
        parts.append(
            f"ARMED KILL: worker `{pending_kill}` is armed. If the supervisor confirms, call confirm_kill now."
        )
    return "\n".join(parts) + "\n"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "fleet_status",
            "description": "Get live fleet status: workers, statuses, PIDs.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pause_worker",
            "description": "Pause a running worker process.",
            "parameters": {
                "type": "object",
                "properties": {
                    "worker_id": {
                        "type": "string",
                        "description": "Worker id or name from the current fleet list.",
                    }
                },
                "required": ["worker_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resume_worker",
            "description": "Resume a paused worker. If the worker is killed/dead, this RESTARTS it — always call this for resume/restart requests.",
            "parameters": {
                "type": "object",
                "properties": {
                    "worker_id": {
                        "type": "string",
                        "description": "Worker id or name from the current fleet list.",
                    }
                },
                "required": ["worker_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kill_worker",
            "description": "Arm a kill for a worker. Requires confirm_kill afterwards.",
            "parameters": {
                "type": "object",
                "properties": {
                    "worker_id": {
                        "type": "string",
                        "description": "Worker id or name from the current fleet list.",
                    }
                },
                "required": ["worker_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_kill",
            "description": "Confirm and execute a previously armed kill.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "redirect_worker",
            "description": "Steer/redirect a worker. For God RT, target is a quiet god-rt verb (campaign_status, brief, list, ready, next, probe) — runs for real. Other workers: set clearance label only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "worker_id": {
                        "type": "string",
                        "description": "Worker id or name from the current fleet list.",
                    },
                    "target": {"type": "string"},
                },
                "required": ["worker_id", "target"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "restart_worker",
            "description": "Restart a demo-owned worker, or resume a discovered process if it is paused.",
            "parameters": {
                "type": "object",
                "properties": {
                    "worker_id": {
                        "type": "string",
                        "description": "Worker id or name from the current fleet list.",
                    }
                },
                "required": ["worker_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_worker",
            "description": "Tail recent log lines for a worker and summarize last activity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "worker_id": {
                        "type": "string",
                        "description": "Worker id or name from the current fleet list.",
                    },
                    "lines": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "description": "How many trailing log lines to read (default 20).",
                    },
                },
                "required": ["worker_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pause_all",
            "description": "Pause running workers. Default scope=demo (hackathon workers only). Does NOT SIGSTOP God Mode / claude session trees. Use scope=god only when the supervisor explicitly says pause god / pause all god.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["demo", "god", "all"],
                        "description": "demo (default) = fake workers only. god = discovered workloads, still excluding session wrappers. all = both.",
                    }
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resume_all",
            "description": "Resume paused workers. Default scope=demo. God workers resume only with scope=god or all. Resume walks the process tree (SIGCONT children, not just the listed PID).",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["demo", "god", "all"],
                        "description": "demo (default) = fake workers only. god = discovered workloads. all = both.",
                    }
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "worker_session",
            "description": "Where a worker is running: tty/terminal device, parent app (Terminal.app / iTerm / Claude.app / god launcher), Claude --resume UUID, cwd, session hint. Use for which terminal / what session / where is X.",
            "parameters": {
                "type": "object",
                "properties": {
                    "worker_id": {
                        "type": "string",
                        "description": "Worker id or name from the current fleet list.",
                    }
                },
                "required": ["worker_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "audit_tail",
            "description": "Read recent prove-it audit receipts.",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 40}},
                "additionalProperties": False,
            },
        },
    },
]


def _providers() -> list[tuple[str, str, str, bool]]:
    out: list[tuple[str, str, str, bool]] = []
    or_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if or_key:
        out.append((OPENROUTER_URL, or_key, OPENROUTER_MODEL, True))
    ds_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if ds_key:
        out.append((DEEPSEEK_URL, ds_key, DEEPSEEK_MODEL, False))
    return out


def _chat(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    last_err: Exception | None = None
    for url, key, model, is_or in _providers():
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        if is_or:
            headers["HTTP-Referer"] = "https://github.com/canboigay/airtraffic-control"
            headers["X-Title"] = "AiRTraffic Control"
        body: dict[str, Any] = {
            "model": model,
            "temperature": 0.4,
            "messages": messages,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        try:
            with httpx.Client(timeout=45.0) as client:
                resp = client.post(url, headers=headers, json=body)
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"LLM unavailable: {last_err}")


def _exec_tool(
    name: str,
    args: dict[str, Any],
    *,
    registry,
    audit_log,
    get_pending,
    set_pending,
    confirm_phrase: str,
    source: str,
    heard: str,
) -> dict[str, Any]:
    if name == "fleet_status":
        summary = registry.status_summary()
        audit_log.record("status", detail=summary, source=source)
        return {"ok": True, "action": "status", "result": summary}

    if name == "pause_worker":
        wid = args["worker_id"]
        result = registry.pause(wid)
        audit_log.record(
            "pause",
            worker_id=wid,
            before=result["before"],
            after=result["after"],
            detail={"transcript": heard},
            source=source,
        )
        return {"ok": True, "action": "pause", "result": result}

    if name == "resume_worker":
        wid = args["worker_id"]
        result = registry.resume(wid)
        audit_log.record(
            "resume",
            worker_id=wid,
            before=result["before"],
            after=result["after"],
            detail={"transcript": heard},
            source=source,
        )
        return {"ok": True, "action": "resume", "result": result}

    if name == "kill_worker":
        wid = args["worker_id"]
        result = registry.request_kill(wid)
        set_pending(result["worker_id"])
        audit_log.record(
            "kill.arm",
            worker_id=result["worker_id"],
            before=result["before"],
            detail={"confirm_phrase": confirm_phrase, "transcript": heard},
            source=source,
        )
        return {"ok": True, "action": "kill.arm", "result": result}

    if name == "confirm_kill":
        wid = get_pending()
        if not wid:
            return {
                "ok": False,
                "action": "confirm_kill",
                "error": "No kill armed. Kill a worker first, then confirm.",
            }
        try:
            result = registry.execute_kill(wid, confirm_phrase)
        except PermissionError as e:
            audit_log.record("kill.denied", worker_id=wid, detail={"reason": str(e)}, source=source)
            return {"ok": False, "action": "confirm_kill", "error": str(e)}
        set_pending(None)
        audit_log.record(
            "kill",
            worker_id=wid,
            before=result["before"],
            after=result["after"],
            detail={"confirmed": True, "transcript": heard},
            source=source,
        )
        return {"ok": True, "action": "kill", "result": result}

    if name == "redirect_worker":
        wid = args["worker_id"]
        target = args["target"]
        result = registry.redirect(wid, target)
        audit_log.record(
            "redirect",
            worker_id=wid,
            before=result["before"],
            after=result["after"],
            detail={"target": target, "transcript": heard},
            source=source,
        )
        return {"ok": True, "action": "redirect", "result": result}

    if name == "restart_worker":
        wid = args["worker_id"]
        result = registry.restart(wid)
        audit_log.record(
            "restart",
            worker_id=wid,
            before=result["before"],
            after=result["after"],
            detail={"transcript": heard},
            source=source,
        )
        return {"ok": True, "action": "restart", "result": result}

    if name == "inspect_worker":
        wid = args["worker_id"]
        lines = int(args.get("lines") or 20)
        result = registry.inspect_worker(wid, lines=lines)
        audit_log.record(
            "inspect",
            worker_id=result.get("worker_id") or wid,
            detail={"lines": lines, "summary": result.get("summary"), "transcript": heard},
            source=source,
        )
        return {"ok": True, "action": "inspect", "result": result}

    if name == "pause_all":
        scope = str(args.get("scope") or "demo")
        result = registry.pause_all(scope=scope)
        audit_log.record(
            "pause_all",
            detail={"paused_count": result.get("paused_count"), "scope": result.get("scope"), "transcript": heard},
            source=source,
        )
        return {"ok": True, "action": "pause_all", "result": result}

    if name == "resume_all":
        scope = str(args.get("scope") or "demo")
        result = registry.resume_all(scope=scope)
        audit_log.record(
            "resume_all",
            detail={"resumed_count": result.get("resumed_count"), "scope": result.get("scope"), "transcript": heard},
            source=source,
        )
        return {"ok": True, "action": "resume_all", "result": result}

    if name == "worker_session":
        from backend.session_insight import spoken_session_answer

        wid = args["worker_id"]
        snap = registry.get(wid)
        if not snap:
            return {"ok": False, "action": "session", "error": f"unknown worker: {wid}"}
        resolved = snap.get("id") or wid
        summary = spoken_session_answer(snap)
        result = {
            "worker_id": resolved,
            "worker": snap,
            "session_tty": snap.get("session_tty"),
            "session_app": snap.get("session_app"),
            "session_id": snap.get("session_id"),
            "session_cwd": snap.get("session_cwd"),
            "session_hint": snap.get("session_hint"),
            "summary": summary,
        }
        audit_log.record(
            "session",
            worker_id=resolved,
            detail={"summary": summary, "transcript": heard},
            source=source,
        )
        return {"ok": True, "action": "session", "result": result}

    if name == "audit_tail":
        limit = int(args.get("limit") or 12)
        entries = audit_log.list(limit)
        return {"ok": True, "action": "audit", "result": {"entries": entries}}

    return {"ok": False, "error": f"unknown tool: {name}"}



def sanitize_spoken(text: str, *, limit: int = 180, max_words: int = 22) -> str:
    """Make LLM reply safe for JSON + TTS (short + speakable)."""
    s = (text or "").strip()
    if not s:
        return "Copy."
    s = re.sub(r"```[\s\S]*?```", " ", s)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"\*([^*]+)\*", r"\1", s)
    s = re.sub(r"^\s*[-•]\s+", "", s, flags=re.M)
    s = re.sub(r"[\r\n]+", " ", s)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.strip("\"'")
    if not s:
        return "Copy."
    parts = re.split(r"(?<=[.!?])\s+", s)
    s = " ".join(parts[:2]).strip()
    words = s.split()
    if len(words) > max_words:
        s = " ".join(words[:max_words]).rstrip(",;:") + "."
    return s[:limit]




def _match_worker_in_text(text: str, workers: list[dict[str, Any]]) -> str | None:
    """Resolve a worker id from spoken text against the live fleet."""
    t = " ".join((text or "").lower().split())
    if not t:
        return None
    # prefer longer name/id matches
    ranked: list[tuple[int, str]] = []
    for w in workers:
        wid = str(w.get("id") or "")
        name = str(w.get("name") or "")
        for cand in {wid, name, wid.replace("-", " "), name.lower()}:
            c = " ".join(cand.lower().split())
            if c and c in t:
                ranked.append((len(c), wid))
        pid = w.get("pid")
        if pid is not None and re.search(rf"\b{pid}\b", t):
            ranked.append((10, wid))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    return ranked[0][1]


def _spoken_from_tool(payload: dict[str, Any]) -> str:
    action = payload.get("action")
    if not payload.get("ok"):
        return str(payload.get("error") or "Unable.")
    result = payload.get("result") or {}
    after = result.get("after") or {}
    before = result.get("before") or {}
    name = after.get("name") or before.get("name") or result.get("worker_id") or "Worker"
    pid = after.get("pid") or before.get("pid")
    if action == "status":
        workers = result.get("workers") or []
        n = len(workers)
        paused = [w.get("name") for w in workers if w.get("status") == "paused"]
        killed = [w.get("name") for w in workers if w.get("status") == "killed"]
        if paused or killed:
            bits = []
            if paused:
                bits.append("paused " + ", ".join(paused))
            if killed:
                bits.append("killed " + ", ".join(killed))
            return f"{n} workers. " + "; ".join(bits) + "."
        return f"{n} workers running, all green."
    if action == "kill.arm":
        return f"{name} kill armed. Say confirm kill."
    if action == "kill":
        return f"{name} terminated."
    if action == "pause":
        return f"{name} paused" + (f", pid {pid}" if pid else "") + "."
    if action in {"resume", "restart"}:
        return f"{name} back online" + (f", pid {pid}" if pid else "") + "."
    if action == "redirect":
        steer = result.get("steer") or {}
        if steer.get("verb") and steer.get("ok"):
            summary = steer.get("summary") or steer.get("verb")
            # Keep TTS short
            short = summary if len(summary) <= 90 else summary[:87] + "..."
            return f"{name} steered to {steer.get('verb')}. {short}"
        if steer.get("error"):
            return f"{name} steer failed: {steer.get('error')}"
        tgt = after.get("target") or "new target"
        detail = after.get("detail") or ""
        if detail.startswith("steered"):
            return f"{name} {detail}."
        return f"{name} redirected to {tgt}."
    if action == "pause_all":
        n = int(result.get("paused_count") or 0)
        scope = result.get("scope") or "demo"
        skipped = result.get("skipped") or []
        held = sum(
            1
            for s in skipped
            if any(k in (s.get("reason") or "").lower() for k in ("god", "protected", "session tree", "excluded"))
        )
        if scope == "demo" and held:
            return f"Paused {n} demo workers. God sessions left running."
        return f"Paused {n} workers."
    if action == "resume_all":
        n = int(result.get("resumed_count") or 0)
        return f"Resumed {n} workers."
    if action == "session":
        summary = result.get("summary")
        if summary:
            return sanitize_spoken(str(summary), max_words=28)
        from backend.session_insight import spoken_session_answer

        w = result.get("worker") or {}
        return sanitize_spoken(spoken_session_answer(w), max_words=28)
    if action == "inspect":
        summary = result.get("summary") or "No log activity."
        # Prefer a short speakable form
        wname = (result.get("worker") or {}).get("name") or result.get("worker_id") or "Worker"
        lines = result.get("lines") or []
        if result.get("note") == "no log file":
            return f"{wname}: no log file."
        if lines:
            last = lines[-1].strip()
            # strip common prefixes for speech
            last = re.sub(r"^\[[^\]]+\]\s*", "", last)
            if len(last) > 80:
                last = last[:77] + "..."
            return f"{wname} last logged: {last}."
        return sanitize_spoken(summary, max_words=22)
    return "Done."


def _fast_fleet_action(
    heard: str,
    *,
    registry,
    audit_log,
    get_pending,
    set_pending,
    confirm_phrase: str,
    source: str,
    fleet: list[dict[str, Any]],
    memory=None,
) -> dict[str, Any] | None:
    """Deterministic path for clear tower orders — skips LLM hesitation."""
    cmd = parse_command(heard)
    if not cmd or cmd.action in {"unknown", None}:
        return None

    last_worker = None
    last_action = None
    last_target = None
    last_turn = None
    if memory is not None:
        last_worker = memory.last_worker_id()
        last_action = memory.last_action()
        last_target = memory.last_target()
        lt = memory.last_controllable() or memory.last()
        if lt is not None:
            last_turn = lt.as_dict() if hasattr(lt, "as_dict") else None

    cmd = apply_memory(
        cmd,
        last_worker_id=last_worker,
        last_action=last_action,
        last_target=last_target,
        last_turn=last_turn,
    )

    # Clarify: speak from last tower reply / action without new tools
    if cmd.action == "clarify":
        if not last_turn:
            return {
                "ok": True,
                "action": "clarify",
                "spoken_reply": sanitize_spoken("No prior clearance to explain."),
                "result": None,
                "fast_path": True,
                "from_memory": True,
            }
        prev_spoken = (last_turn.get("spoken_reply") or "").strip()
        prev_action = last_turn.get("action") or "that"
        prev_worker = last_turn.get("worker_id") or ""
        if prev_spoken:
            spoken = f"I meant: {prev_spoken}"
        else:
            spoken = f"Last clearance was {prev_action}" + (f" on {prev_worker}" if prev_worker else "") + "."
        return {
            "ok": True,
            "action": "clarify",
            "spoken_reply": sanitize_spoken(spoken, max_words=28),
            "result": {"recalled": last_turn},
            "fast_path": True,
            "from_memory": True,
        }

    if cmd.action in {"unknown", None}:
        return None

    action = cmd.action
    wid = cmd.worker_id
    if action in {"pause", "resume", "kill", "redirect", "inspect", "session"}:
        # Prefer live fleet match (covers mcp-hands etc.) then rule alias / memory
        live = _match_worker_in_text(heard, fleet)
        if live:
            wid = live
        if not wid and last_worker and cmd.from_memory:
            wid = last_worker
        if not wid:
            return None
        resolved = registry.resolve_id(wid) or wid
        wid = resolved

    tool = None
    args: dict[str, Any] = {}
    if action == "status":
        tool = "fleet_status"
    elif action == "pause":
        tool, args = "pause_worker", {"worker_id": wid}
    elif action == "resume":
        tool, args = "resume_worker", {"worker_id": wid}
    elif action == "kill":
        tool, args = "kill_worker", {"worker_id": wid}
    elif action == "confirm_kill":
        if not get_pending():
            return None
        tool = "confirm_kill"
    elif action == "redirect":
        if not cmd.target:
            return None
        tool, args = "redirect_worker", {"worker_id": wid, "target": cmd.target}
    elif action == "pause_all":
        tool, args = "pause_all", {"scope": cmd.scope or "demo"}
    elif action == "resume_all":
        tool, args = "resume_all", {"scope": cmd.scope or "demo"}
    elif action == "inspect":
        tool, args = "inspect_worker", {"worker_id": wid, "lines": int(cmd.lines or 20)}
    elif action == "session":
        tool, args = "worker_session", {"worker_id": wid}
    else:
        return None
    try:
        payload = _exec_tool(
            tool,
            args,
            registry=registry,
            audit_log=audit_log,
            get_pending=get_pending,
            set_pending=set_pending,
            confirm_phrase=confirm_phrase,
            source=source,
            heard=heard,
        )
    except Exception as e:
        payload = {"ok": False, "action": action, "error": str(e)}
    spoken = _spoken_from_tool(payload)
    out = {
        "ok": bool(payload.get("ok", True)),
        "action": payload.get("action") or action,
        "spoken_reply": sanitize_spoken(spoken),
        "result": payload.get("result"),
        "tool_trace": [{"tool": tool, "args": args, "result": payload}],
        "fast_path": True,
        "worker_id": wid,
        "target": cmd.target,
    }
    if cmd.from_memory:
        out["from_memory"] = True
        out["memory_ref"] = cmd.memory_ref
    if payload.get("error"):
        out["error"] = payload["error"]
    # Surface steer on redirect
    if isinstance(payload.get("result"), dict) and payload["result"].get("steer"):
        out["steer"] = payload["result"]["steer"]
    return out


def _looks_like_confirm(heard: str) -> bool:
    h = " ".join((heard or "").lower().split())
    if not h:
        return False
    if h in {"confirm kill", "confirm", "yes confirm", "yes", "do it", "execute kill", "kill confirmed"}:
        return True
    if h.startswith("confirm") and "kill" in h:
        return True
    return False


def _extract_memory_fields(out: dict[str, Any], heard: str) -> dict[str, Any]:
    """Pull worker_id / target / action for session memory recording."""
    action = out.get("action")
    worker_id = out.get("worker_id")
    target = out.get("target")
    result = out.get("result") or {}
    if not worker_id:
        after = result.get("after") or {}
        before = result.get("before") or {}
        worker_id = after.get("id") or before.get("id") or result.get("worker_id")
    if not target:
        after = result.get("after") or {}
        target = after.get("target") or (result.get("steer") or {}).get("label")
    if out.get("steer") and not target:
        target = (out.get("steer") or {}).get("label")
    return {
        "heard": heard,
        "spoken_reply": out.get("spoken_reply") or "",
        "action": action,
        "worker_id": worker_id,
        "target": target,
        "ok": out.get("ok"),
    }


def run_tower_agent(
    heard: str,
    *,
    registry,
    audit_log,
    get_pending: Callable[[], str | None],
    set_pending: Callable[[str | None], None],
    confirm_phrase: str,
    source: str = "voice",
    max_rounds: int = 4,
    memory=None,
) -> dict[str, Any]:
    heard = (heard or "").strip()
    if not heard:
        return {"ok": False, "action": "chat", "error": "empty utterance", "spoken_reply": sanitize_spoken("Say again.")}

    fleet = []
    try:
        fleet = registry.list_workers()
    except Exception:
        fleet = []

    pending = None
    try:
        pending = get_pending()
    except Exception:
        pending = None

    memory_block = None
    if memory is not None:
        try:
            memory_block = memory.prompt_block()
        except Exception:
            memory_block = None

    # Deterministic confirm path — do not rely on the LLM remembering prior turns
    if pending and _looks_like_confirm(heard):
        payload = _exec_tool(
            "confirm_kill",
            {},
            registry=registry,
            audit_log=audit_log,
            get_pending=get_pending,
            set_pending=set_pending,
            confirm_phrase=confirm_phrase,
            source=source,
            heard=heard,
        )
        if payload.get("ok"):
            after = (payload.get("result") or {}).get("after") or {}
            name = after.get("name") or pending
            spoken = f"{name} terminated."
        else:
            spoken = payload.get("error") or "Kill confirm failed."
        out = {
            "ok": bool(payload.get("ok")),
            "action": payload.get("action") or "confirm_kill",
            "spoken_reply": sanitize_spoken(spoken),
            "result": payload.get("result"),
            "error": payload.get("error"),
            "tool_trace": [{"tool": "confirm_kill", "args": {}, "result": payload}],
            "worker_id": pending,
        }
        if memory is not None:
            memory.record(**_extract_memory_fields(out, heard), source=source)
        return out

    fast = _fast_fleet_action(
        heard,
        registry=registry,
        audit_log=audit_log,
        get_pending=get_pending,
        set_pending=set_pending,
        confirm_phrase=confirm_phrase,
        source=source,
        fleet=fleet,
        memory=memory,
    )
    if fast is not None:
        if memory is not None:
            memory.record(**_extract_memory_fields(fast, heard), source=source)
        return fast

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _system_prompt(fleet, pending, memory_block)},
        {"role": "user", "content": heard},
    ]

    tool_trace: list[dict[str, Any]] = []
    last_tool_payload: dict[str, Any] | None = None

    for _ in range(max_rounds):
        msg = _chat(messages, TOOLS)
        tool_calls = msg.get("tool_calls") or []
        content = (msg.get("content") or "").strip()

        if not tool_calls:
            spoken = content or "Copy."
            spoken = spoken.strip("\"'")
            action = (last_tool_payload or {}).get("action") or "chat"
            ok = True if last_tool_payload is None else bool((last_tool_payload or {}).get("ok", True))
            out: dict[str, Any] = {
                "ok": ok,
                "action": action if last_tool_payload else "chat",
                "spoken_reply": sanitize_spoken(spoken),
                "result": (last_tool_payload or {}).get("result"),
                "tool_trace": tool_trace,
            }
            if last_tool_payload and last_tool_payload.get("error"):
                out["error"] = last_tool_payload["error"]
            if memory is not None:
                memory.record(**_extract_memory_fields(out, heard), source=source)
            return out

        # Append assistant tool-call message
        messages.append(
            {
                "role": "assistant",
                "content": msg.get("content"),
                "tool_calls": tool_calls,
            }
        )

        for call in tool_calls:
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except json.JSONDecodeError:
                args = {}
            try:
                payload = _exec_tool(
                    name,
                    args,
                    registry=registry,
                    audit_log=audit_log,
                    get_pending=get_pending,
                    set_pending=set_pending,
                    confirm_phrase=confirm_phrase,
                    source=source,
                    heard=heard,
                )
            except Exception as e:
                payload = {"ok": False, "error": str(e), "action": name}
            last_tool_payload = payload
            tool_trace.append({"tool": name, "args": args, "result": payload})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id") or name,
                    "content": json.dumps(payload, default=str),
                }
            )

    # Exhausted rounds — summarize from last tool
    summary = "Done."
    if last_tool_payload:
        summary = json.dumps(last_tool_payload, default=str)[:300]
    out_final = {
        "ok": bool((last_tool_payload or {}).get("ok", True)),
        "action": (last_tool_payload or {}).get("action") or "chat",
        "spoken_reply": sanitize_spoken(summary if isinstance(summary, str) else str(summary)),
        "result": (last_tool_payload or {}).get("result"),
        "tool_trace": tool_trace,
    }
    if memory is not None:
        memory.record(**_extract_memory_fields(out_final, heard), source=source)
    return out_final
