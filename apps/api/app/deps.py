from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import get_redis
from app.db import get_db
from app.models import AuthSession, User
from app.security import SESSION_COOKIE, read_session_jwt


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    user = await optional_user(request, db)
    if user is None:
        raise HTTPException(401, "Not signed in")
    return user


async def optional_user(request: Request, db: AsyncSession = Depends(get_db)) -> User | None:
    token = request.cookies.get(SESSION_COOKIE)
    claims = read_session_jwt(token) if token else None
    if not claims:
        return None
    sid = uuid.UUID(claims["sid"])
    cached = None
    revoked = None
    r = None
    try:
        r = get_redis()
        revoked = await r.get(f"revoked:{sid}")
        cached = await r.get(f"sess:{sid}")
    except Exception:  # noqa: BLE001 — Redis outage must not 500 login or /me
        r = None
    if revoked:
        return None
    if not cached:
        sess = await db.get(AuthSession, sid)
        if sess is None or sess.revoked or sess.expires_at < datetime.now(timezone.utc):
            return None
        if r is not None:
            try:
                await r.set(f"sess:{sid}", b"1", ex=300)
            except Exception:  # noqa: BLE001
                pass
    user = await db.get(User, uuid.UUID(claims["sub"]))
    if user is not None:
        request.state.auth_session_id = sid
    return user
