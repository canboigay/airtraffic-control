#!/usr/bin/env bash
# AiRTraffic Control — boot backend + frontend + demo workers
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8765}"
# hybrid = demo workers + discovered God Mode stack processes (default for local)
export ATC_ADAPTER="${ATC_ADAPTER:-hybrid}"

# Load Speechmatics key from Mac Keychain into .env without echoing the value

load_llm_keys() {
  if [[ "$(uname -s)" != "Darwin" ]]; then
    return 0
  fi
  if ! command -v security >/dev/null 2>&1; then
    return 0
  fi
  touch .env
  local key
  if key="$(security find-generic-password -s god-mode -a openrouter -w 2>/dev/null)"; then
    if [[ -n "$key" ]]; then
      grep -v '^OPENROUTER_API_KEY=' .env > .env.tmp || true
      mv .env.tmp .env
      printf 'OPENROUTER_API_KEY=%s
' "$key" >> .env
      unset key
      echo "[start] Loaded OPENROUTER_API_KEY from Keychain"
    fi
  fi
  if key="$(security find-generic-password -s god-mode -a deepseek -w 2>/dev/null)"; then
    if [[ -n "$key" ]]; then
      grep -v '^DEEPSEEK_API_KEY=' .env > .env.tmp || true
      mv .env.tmp .env
      printf 'DEEPSEEK_API_KEY=%s
' "$key" >> .env
      unset key
      echo "[start] Loaded DEEPSEEK_API_KEY from Keychain"
    fi
  fi
}

load_keychain_key() {
  if [[ "$(uname -s)" != "Darwin" ]]; then
    return 0
  fi
  if [[ -f .env ]] && grep -q '^SPEECHMATICS_API_KEY=.\+' .env 2>/dev/null; then
    echo "[start] SPEECHMATICS_API_KEY already set in .env"
    return 0
  fi
  if ! command -v security >/dev/null 2>&1; then
    echo "[start] security(1) not found; skip Keychain load"
    return 0
  fi
  local key
  if key="$(security find-generic-password -s god-mode -a speechmatics -w 2>/dev/null)"; then
    if [[ -n "$key" ]]; then
      # Write/update .env without printing the secret
      if [[ -f .env ]]; then
        grep -v '^SPEECHMATICS_API_KEY=' .env > .env.tmp || true
        mv .env.tmp .env
      else
        touch .env
      fi
      printf 'SPEECHMATICS_API_KEY=%s\n' "$key" >> .env
      unset key
      echo "[start] Loaded SPEECHMATICS_API_KEY from Keychain (god-mode / speechmatics) into .env"
    fi
  else
    echo "[start] Keychain entry not found (service=god-mode account=speechmatics)."
    echo "[start] Set SPEECHMATICS_API_KEY in .env manually (see .env.example)."
  fi
}

ensure_venv() {
  if [[ ! -d .venv ]]; then
    echo "[start] Creating .venv…"
    python3 -m venv .venv
  fi
  # shellcheck disable=SC1091
  source .venv/bin/activate
  python -m pip install -q -r requirements.txt
}

load_keychain_key
load_llm_keys
ensure_venv

mkdir -p logs
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

# Export key for this process without printing
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [[ -z "${SPEECHMATICS_API_KEY:-}" ]]; then
  echo "[start] WARNING: SPEECHMATICS_API_KEY unset — voice JWT mint will fail."
  echo "[start] Text fallback and worker controls still work."
fi

echo "[start] AiRTraffic Control at http://${HOST}:${PORT}"
echo "[start] Adapter mode: ${ATC_ADAPTER} (local=God Mode discovery, demo=3 fake workers, hybrid=both)"
if [[ "${ATC_ADAPTER}" == "demo" || "${ATC_ADAPTER}" == "hybrid" ]]; then
  echo "[start] Demo workers spawn on API startup (log-spam, fake-build, fake-research)"
fi
if [[ "${ATC_ADAPTER}" == "local" || "${ATC_ADAPTER}" == "hybrid" ]]; then
  echo "[start] Discovering God Mode stack processes under ${GOD_STACK:-/Users/simeong/local-claude-offline-stack}"
fi
exec python -m uvicorn backend.main:app --host "$HOST" --port "$PORT"
