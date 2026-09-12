#!/usr/bin/env bash
# AeroVoice Flight Controller — boot backend + frontend + demo workers
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8765}"

# Load Speechmatics key from Mac Keychain into .env without echoing the value
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
  pip install -q -r requirements.txt
}

load_keychain_key
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

echo "[start] AeroVoice at http://${HOST}:${PORT}"
echo "[start] Demo workers spawn on API startup (log-spam, fake-build, fake-research)"
exec python -m uvicorn backend.main:app --host "$HOST" --port "$PORT"
