"""Speechmatics Realtime JWT minting.

POST https://mp.speechmatics.com/v1/api_keys?type=rt
Authorization: Bearer <SPEECHMATICS_API_KEY>
"""

from __future__ import annotations

import os
from typing import Any

import httpx

SPEECHMATICS_API_KEYS_URL = "https://mp.speechmatics.com/v1/api_keys"


async def mint_rt_jwt(*, ttl_seconds: int = 3600) -> dict[str, Any]:
    api_key = os.environ.get("SPEECHMATICS_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "SPEECHMATICS_API_KEY is not set. Copy .env.example to .env or use start.sh Keychain load."
        )

    payload = {
        "ttl": ttl_seconds,
        "type": "rt",
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{SPEECHMATICS_API_KEYS_URL}?type=rt",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()

    # Speechmatics returns {"key_value": "<jwt>", ...}
    jwt = data.get("key_value") or data.get("key") or data.get("jwt")
    if not jwt:
        raise RuntimeError(f"Unexpected Speechmatics token response keys: {list(data.keys())}")

    return {
        "jwt": jwt,
        "ttl": ttl_seconds,
        "ws_url": f"wss://global.rt.speechmatics.com/v2?jwt={jwt}",
        "region": "global",
    }
