"""Persist each user's cloud-browser cookies across computer restarts (encrypted, per user, best effort).

The computer's browser profile lives in the sandbox and disappears when the sandbox is recycled. Sites treat a
browser with no history as more suspicious, and the user would have to log in again. So, throttled after
navigations, the API exports the browser's cookies, encrypts them with the app key (``app.security``) and stores
them in ``browser_profiles`` keyed by user id; when a fresh daemon starts on an empty profile they are imported
back. Only cookies (no local storage, history or passwords), capped in size. Any failure is logged and ignored:
browsing never depends on this. BROWSER_PROFILE_PERSIST=0 turns it off.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from app.config import get_settings

log = logging.getLogger(__name__)

MAX_BYTES = 256 * 1024
SAVE_EVERY_S = 300.0
_last_save: dict[str, float] = {}


def enabled(user_id: str) -> bool:
    if not bool(getattr(get_settings(), "browser_profile_persist", True)):
        return False
    try:
        uuid.UUID(str(user_id))
    except Exception:
        return False
    return True


def trim_cookies(cookies: list[Any], now: float | None = None) -> list[dict]:
    """Drop expired and malformed cookies; keep the newest under MAX_BYTES."""
    now = time.time() if now is None else now
    keep: list[dict] = []
    for c in cookies or []:
        if not isinstance(c, dict) or not c.get("name") or not c.get("domain"):
            continue
        exp = c.get("expires")
        if isinstance(exp, (int, float)) and 0 < exp < now:
            continue
        row = {k: c[k] for k in ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite") if k in c}
        keep.append(row)
    while keep and len(json.dumps(keep)) > MAX_BYTES:
        keep = keep[len(keep) // 10 or 1 :]
    return keep


async def load(user_id: str) -> list[dict]:
    if not enabled(user_id):
        return []
    try:
        from app.db import SessionLocal
        from app.models import BrowserProfile
        from app.security import decrypt

        async with SessionLocal() as db:
            row = await db.get(BrowserProfile, uuid.UUID(str(user_id)))
            raw = decrypt(row.state_enc) if row and row.state_enc else None
        data = json.loads(raw) if raw else {}
        return trim_cookies(data.get("cookies") or [])
    except Exception as exc:  # noqa: BLE001
        log.info("browser profile load skipped: %s", type(exc).__name__)
        return []


async def save(user_id: str, cookies: list[Any]) -> bool:
    if not enabled(user_id):
        return False
    rows = trim_cookies(cookies)
    if not rows:
        return False
    try:
        from app.db import SessionLocal
        from app.models import BrowserProfile
        from app.security import encrypt

        uid = uuid.UUID(str(user_id))
        blob = encrypt({"cookies": rows, "v": 1})
        async with SessionLocal() as db:
            row = await db.get(BrowserProfile, uid)
            if row is None:
                db.add(BrowserProfile(user_id=uid, state_enc=blob, cookie_count=len(rows)))
            else:
                row.state_enc = blob
                row.cookie_count = len(rows)
            await db.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        log.info("browser profile save skipped: %s", type(exc).__name__)
        return False


def save_soon(user_id: str) -> None:
    """Drop the throttle so the next save runs (after a sign-in, so a recycled computer stays signed in)."""
    _last_save.pop(str(user_id), None)


def due(user_id: str, now: float | None = None) -> bool:
    """Throttle: at most one export per user every SAVE_EVERY_S."""
    if not enabled(user_id):
        return False
    now = time.monotonic() if now is None else now
    last = _last_save.get(str(user_id))
    if last is not None and now - last < SAVE_EVERY_S:
        return False
    _last_save[str(user_id)] = now
    return True
