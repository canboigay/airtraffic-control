"""Quiet god-rt steer path — allowlisted read/status verbs only.

Voice, text, and UI redirect all hit this for God RT workers.
Pause/kill stay emergency stops on the process; Claude/Cursor/Grok stay denylisted
in discovery. No engage / exploit / GO / loud promote from ATC.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

DEFAULT_STACK = "/Users/simeong/local-claude-offline-stack"
DEFAULT_TIMEOUT = float(os.environ.get("ATC_GOD_RT_TIMEOUT", "45"))

# Canonical verb -> argv tokens after god-rt
# Aliases normalize spoken/text targets into these.
CANONICAL: dict[str, tuple[str, ...]] = {
    "campaign_status": ("campaign_status",),
    "brief": ("brief",),
    "campaign_brief": ("brief",),
    "campaign_list": ("campaign_list",),
    "list": ("campaign_list",),
    "ready": ("ready",),
    "next": ("next",),
    # bare "probe" = read-only probe overview via campaign_status (no write)
    "probe": ("campaign_status",),
    "probes": ("campaign_status",),
}

ALIAS_TO_CANON = {
    "campaign_status": "campaign_status",
    "campaign-status": "campaign_status",
    "campaign status": "campaign_status",
    "status": "campaign_status",
    "sitrep": "campaign_status",
    "brief": "brief",
    "campaign_brief": "campaign_brief",
    "campaign-brief": "campaign_brief",
    "campaign brief": "campaign_brief",
    "list": "list",
    "campaign_list": "campaign_list",
    "campaign-list": "campaign_list",
    "campaign list": "campaign_list",
    "campaigns": "campaign_list",
    "ready": "ready",
    "preflight": "ready",
    "next": "next",
    "probe": "probe",
    "probes": "probes",
}

# Hard deny — never invoke from ATC steer
DENY_VERBS = frozenset(
    {
        "engage",
        "go",
        "loud",
        "promote",
        "exploit_auto",
        "exploit_h2te",
        "exploit_badhost",
        "exploit_litellm_mcp",
        "exploit_oob_start",
        "exploit_oob_poll",
        "exploit_oob_stop",
        "recon_full",
        "recon_surface",
        "recon_classify",
        "campaign_init",
        "campaign_recon",
        "campaign_exploit",
        "campaign_chain",
        "campaign_finding",
        "campaign_verdict",
        "campaign_wave",
        "campaign_probe",  # write path — not via ATC
        "campaign_probe_close",
        "opsec_up",
        "tokenhunt",
        "scafu",
        "optional",
        "watch",
    }
)

Runner = Callable[[list[str], float], subprocess.CompletedProcess[str]]


@dataclass
class SteerRequest:
    verb: str
    argv: list[str]
    raw_target: str
    extra_args: list[str] = field(default_factory=list)


@dataclass
class SteerResult:
    ok: bool
    verb: str | None = None
    argv: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    summary: str = ""
    error: str | None = None
    label: str | None = None  # persisted target label

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "verb": self.verb,
            "argv": list(self.argv),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "summary": self.summary,
            "error": self.error,
            "label": self.label,
        }


def _norm_key(text: str) -> str:
    t = (text or "").strip().lower()
    t = t.replace("_", " ").replace("-", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def parse_steer_target(target: str) -> SteerRequest | None:
    """Map a redirect/steer target string to an allowlisted god-rt argv.

    Returns None if the target is not a quiet steer verb (caller may keep
    label-only semantics). Raises ValueError if the verb is explicitly denied.
    """
    raw = (target or "").strip()
    if not raw:
        raise ValueError("empty steer target")

    # Try to tokenize; keep optional trailing args (e.g. brief zeeh.africa)
    try:
        parts = shlex.split(raw)
    except ValueError:
        parts = raw.split()
    if not parts:
        raise ValueError("empty steer target")

    first = parts[0].strip().lower().replace("-", "_")
    # Multi-word alias: "campaign status", "campaign list", "campaign brief"
    joined2 = _norm_key(" ".join(parts[:2])) if len(parts) >= 2 else ""
    joined1 = _norm_key(parts[0])

    canon: str | None = None
    rest: list[str] = []

    if joined2 in ALIAS_TO_CANON:
        canon = ALIAS_TO_CANON[joined2]
        rest = parts[2:]
    elif joined1 in ALIAS_TO_CANON:
        canon = ALIAS_TO_CANON[joined1]
        rest = parts[1:]
    elif first in ALIAS_TO_CANON:
        canon = ALIAS_TO_CANON[first]
        rest = parts[1:]
    else:
        # Underscore form of first token
        unders = first.replace(" ", "_")
        if unders in ALIAS_TO_CANON:
            canon = ALIAS_TO_CANON[unders]
            rest = parts[1:]

    if canon is None:
        # Explicit deny on unknown first token that looks like a god-rt verb
        deny_key = first.replace(" ", "_")
        if deny_key in DENY_VERBS or any(deny_key.startswith(d) for d in ("exploit", "recon_", "engage")):
            raise ValueError(
                f"steer denied: {parts[0]!r} is not an allowlisted quiet god-rt verb "
                f"(use campaign_status, brief, list, ready, next, probe)"
            )
        return None

    if canon in DENY_VERBS or CANONICAL.get(canon, (canon,))[0] in DENY_VERBS:
        raise ValueError(f"steer denied: {canon!r}")

    argv_prefix = list(CANONICAL[canon])
    # Strip accidental "to" / filler from voice
    rest = [a for a in rest if a.lower() not in {"to", "please", "now"}]
    return SteerRequest(verb=canon, argv=argv_prefix + rest, raw_target=raw, extra_args=rest)


def summarize_output(text: str, *, limit: int = 280) -> str:
    """One-line-ish summary for TTS / UI from god-rt stdout."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    # Drop ANSI
    cleaned: list[str] = []
    for ln in lines:
        ln = re.sub(r"\x1b\[[0-9;]*m", "", ln)
        if ln.startswith("[opsec_down]"):
            continue
        cleaned.append(ln)
    if not cleaned:
        return "god-rt returned no output."
    # Prefer BRIEF / STATUS / CAMPAIGN STATUS header lines
    pick = cleaned[0]
    for ln in cleaned:
        up = ln.upper()
        if up.startswith("BRIEF") or up.startswith("STATUS") or "CAMPAIGN STATUS" in up or up.startswith("GOD-RT READY"):
            pick = ln
            break
    pick = re.sub(r"\s+", " ", pick).strip()
    if len(pick) > limit:
        pick = pick[: limit - 3] + "..."
    return pick


def default_runner(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(Path(cmd[0]).resolve().parent) if cmd else None,
        env={**os.environ, "TERM": "dumb"},
    )


def run_quiet_steer(
    target: str,
    *,
    stack: str | None = None,
    timeout: float | None = None,
    runner: Runner | None = None,
) -> SteerResult:
    """Execute an allowlisted quiet god-rt verb. Honest error if binary missing/down."""
    stack_path = (stack or os.environ.get("GOD_STACK") or DEFAULT_STACK).rstrip("/")
    binary = Path(stack_path) / "god-rt"
    label = (target or "").strip()

    try:
        req = parse_steer_target(target)
    except ValueError as e:
        return SteerResult(ok=False, error=str(e), label=label)

    if req is None:
        # Not a steer verb — caller should treat as label-only
        return SteerResult(
            ok=True,
            verb=None,
            argv=[],
            summary="label-only",
            label=label,
            error=None,
        )

    if not binary.is_file():
        return SteerResult(
            ok=False,
            verb=req.verb,
            argv=list(req.argv),
            error=f"god-rt not found at {binary} (is GOD_STACK set?)",
            label=label,
        )
    if not os.access(binary, os.X_OK):
        return SteerResult(
            ok=False,
            verb=req.verb,
            argv=list(req.argv),
            error=f"god-rt at {binary} is not executable",
            label=label,
        )

    cmd = [str(binary), *req.argv]
    to = float(timeout if timeout is not None else DEFAULT_TIMEOUT)
    run = runner or default_runner
    try:
        proc = run(cmd, to)
    except subprocess.TimeoutExpired:
        return SteerResult(
            ok=False,
            verb=req.verb,
            argv=list(req.argv),
            error=f"god-rt timed out after {to:.0f}s",
            label=label,
        )
    except FileNotFoundError:
        return SteerResult(
            ok=False,
            verb=req.verb,
            argv=list(req.argv),
            error="god-rt launcher missing (zsh/env)",
            label=label,
        )
    except OSError as e:
        return SteerResult(
            ok=False,
            verb=req.verb,
            argv=list(req.argv),
            error=f"god-rt failed to start: {e}",
            label=label,
        )

    out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    summary = summarize_output(proc.stdout or proc.stderr or "")
    ok = proc.returncode == 0
    err = None if ok else (summarize_output(proc.stderr or proc.stdout or "god-rt exited non-zero") or "god-rt error")
    return SteerResult(
        ok=ok,
        verb=req.verb,
        argv=list(req.argv),
        stdout=(proc.stdout or "")[-4000:],
        stderr=(proc.stderr or "")[-2000:],
        exit_code=proc.returncode,
        summary=summary,
        error=err,
        label=label,
    )


def is_god_rt_worker(worker_id: str, command: str | None = None) -> bool:
    wid = (worker_id or "").lower()
    if wid.startswith("god-rt") or wid == "god-rt" or "god-rt-" in wid:
        return True
    if command and "god-rt" in command.lower():
        return True
    return False
