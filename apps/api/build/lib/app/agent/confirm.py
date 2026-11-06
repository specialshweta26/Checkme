from __future__ import annotations

import hashlib
import hmac
import json
import time

from app.config import get_settings


def _key() -> bytes:
    return get_settings().session_secret.encode()


def sign_confirm(kind: str, payload: dict, ttl: int = 600) -> str:
    body = json.dumps({"kind": kind, "payload": payload, "exp": int(time.time()) + ttl}, sort_keys=True)
    sig = hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()
    return f"{sig}.{body}"


def read_confirm(token: str) -> dict | None:
    try:
        sig, body = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    data = json.loads(body)
    if data.get("exp", 0) < time.time():
        return None
    return data
