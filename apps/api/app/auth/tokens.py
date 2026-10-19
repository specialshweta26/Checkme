"""Access tokens: cached in Redis, refreshed under a lock so concurrent requests refresh once."""

from __future__ import annotations

import asyncio
import json
import time
import uuid

from sqlalchemy import select

from app.auth.providers import get_provider
from app.cache import get_redis
from app.db import SessionLocal
from app.models import Account, Connector
from app.security import decrypt, decrypt_json, encrypt


class ReauthRequired(Exception):
    pass


async def _cached_or_refresh(cache_key: str, refresher) -> str:
    r = get_redis()
    hit = await r.get(cache_key)
    if hit:
        return hit.decode()
    lock = f"lock:{cache_key}"
    if await r.set(lock, b"1", nx=True, ex=15):
        try:
            token, ttl = await refresher()
            await r.set(cache_key, token.encode(), ex=max(60, ttl - 120))
            return token
        finally:
            await r.delete(lock)
    for _ in range(30):
        await asyncio.sleep(0.2)
        hit = await r.get(cache_key)
        if hit:
            return hit.decode()
    token, ttl = await refresher()
    await r.set(cache_key, token.encode(), ex=max(60, ttl - 120))
    return token


async def seed_access_token(account_id: uuid.UUID, token: str, expires_in: int) -> None:
    await get_redis().set(f"at:acct:{account_id}", token.encode(), ex=max(60, expires_in - 120))


async def account_access_token(account_id: uuid.UUID) -> str:
    async def refresher():
        async with SessionLocal() as db:
            acct = await db.get(Account, account_id)
            if acct is None:
                raise ReauthRequired("account removed")
            refresh = decrypt(acct.refresh_token_enc)
            if not refresh:
                raise ReauthRequired("no refresh token")
            try:
                data = await get_provider(acct.provider).refresh(refresh)
            except PermissionError as exc:
                await _mark_account_connectors(db, account_id, "auth_expired", str(exc))
                raise ReauthRequired(str(exc)) from exc
            if data.get("refresh_token") and data["refresh_token"] != refresh:
                acct.refresh_token_enc = encrypt(data["refresh_token"])
                await db.commit()
            return data["access_token"], int(data.get("expires_in", 3600))

    return await _cached_or_refresh(f"at:acct:{account_id}", refresher)


async def _mark_account_connectors(db, account_id: uuid.UUID, health: str, detail: str) -> None:
    rows = (await db.execute(select(Connector).where(Connector.account_id == account_id))).scalars().all()
    for c in rows:
        c.health, c.health_detail = health, detail[:500]
    await db.commit()


async def mcp_access_token(connector_id: uuid.UUID) -> str:
    """For MCP connectors using OAuth. Credentials are stored encrypted on the connector row."""
    from app.connectors.mcp_oauth import refresh_mcp_token

    async def refresher():
        async with SessionLocal() as db:
            conn = await db.get(Connector, connector_id)
            if conn is None:
                raise ReauthRequired("connector removed")
            creds = decrypt_json(conn.credentials_enc)
            if creds.get("access_token") and creds.get("expires_at", 0) > time.time() + 120:
                return creds["access_token"], int(creds["expires_at"] - time.time())
            if not creds.get("refresh_token"):
                if creds.get("access_token") and not creds.get("expires_at"):
                    return creds["access_token"], 3600
                conn.health, conn.health_detail = "auth_expired", "token expired and no refresh token"
                await db.commit()
                raise ReauthRequired("reconnect required")
            try:
                data = await refresh_mcp_token(creds)
            except PermissionError as exc:
                conn.health, conn.health_detail = "auth_expired", str(exc)[:500]
                await db.commit()
                raise ReauthRequired(str(exc)) from exc
            creds["access_token"] = data["access_token"]
            creds["expires_at"] = time.time() + int(data.get("expires_in", 3600))
            if data.get("refresh_token"):
                creds["refresh_token"] = data["refresh_token"]
            conn.credentials_enc = encrypt(json.dumps(creds))
            await db.commit()
            return creds["access_token"], int(data.get("expires_in", 3600))

    return await _cached_or_refresh(f"at:mcp:{connector_id}", refresher)


async def invalidate(connector_id: uuid.UUID | None = None, account_id: uuid.UUID | None = None) -> None:
    r = get_redis()
    if connector_id:
        await r.delete(f"at:mcp:{connector_id}")
    if account_id:
        await r.delete(f"at:acct:{account_id}")
