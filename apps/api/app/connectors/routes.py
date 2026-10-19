from __future__ import annotations

import json
import logging
import secrets
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import invalidate
from app.cache import get_redis
from app.config import get_settings
from app.connectors.mcp_client import hostname_slug
from app.connectors.mcp_oauth import (
    authorize_url,
    discover_auth,
    exchange_mcp_code,
    pack_bearer,
    pack_oauth_creds,
    register_client,
)
from app.connectors.registry import server_key_for
from app.connectors.service import next_slug, sync_in_background
from app.connectors.urlsafety import UnsafeUrl, validate_public_url
from app.db import get_db
from app.deps import current_user
from app.models import Connector, ServerCard, User
from app.security import decrypt_json, encrypt, pkce_pair

log = logging.getLogger(__name__)
router = APIRouter(prefix="/connectors", tags=["connectors"])


class AddMcpBody(BaseModel):
    url: str
    name: str | None = None
    auth_type: str = Field(default="oauth", pattern="^(oauth|bearer|header|none)$")
    token: str | None = None
    header: str = "Authorization"
    client_id: str | None = None
    client_secret: str | None = None
    alias: str | None = None
    description: str | None = None


class PatchConnector(BaseModel):
    enabled: bool | None = None
    alias: str | None = None
    description: str | None = None
    default_for: list[str] | None = None


def _public(c: Connector, card: ServerCard | None = None) -> dict:
    return {
        "id": str(c.id),
        "name": c.name,
        "alias": c.alias,
        "description": c.description or "",
        "slug": c.slug,
        "kind": c.kind,
        "provider": c.provider,
        "enabled": c.enabled,
        "health": c.health,
        "health_detail": c.health_detail,
        "tags": c.tags,
        "default_for": c.default_for,
        "server_url": c.server_url,
        "auth_type": c.auth_type,
        "tool_count": card.tool_count if card else None,
        "card": card.card if card else None,
    }


@router.get("")
async def list_connectors(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Connector).where(Connector.user_id == user.id))).scalars().all()
    cards = {}
    keys = [r.server_key for r in rows if r.server_key]
    if keys:
        for card in (await db.execute(select(ServerCard).where(ServerCard.server_key.in_(keys)))).scalars():
            cards[card.server_key] = card
    return {"connectors": [_public(c, cards.get(c.server_key or "")) for c in rows]}


@router.post("")
async def add_connector(
    body: AddMcpBody,
    background: BackgroundTasks,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        url = await validate_public_url(body.url)
    except UnsafeUrl as exc:
        raise HTTPException(400, str(exc))
    slug = await next_slug(db, user.id, body.name or hostname_slug(url))
    name = body.name or hostname_slug(url)
    row = Connector(
        user_id=user.id,
        kind="mcp",
        provider="mcp",
        name=name,
        slug=slug,
        alias=body.alias,
        description=(body.description or "").strip(),
        server_url=url,
        transport="streamable_http",
        auth_type=body.auth_type,
        server_key=server_key_for("mcp", "mcp", url),
        health="pending",
    )
    if body.auth_type in ("bearer", "header"):
        if not body.token:
            raise HTTPException(400, "token is required for bearer/header auth")
        row.credentials_enc = encrypt(pack_bearer(body.token, body.header, "" if body.auth_type == "header" else "Bearer "))
        db.add(row)
        await db.commit()
        background.add_task(sync_in_background, row.id, True)
        return {"connector": _public(row), "oauth_url": None}
    if body.auth_type == "none":
        db.add(row)
        await db.commit()
        background.add_task(sync_in_background, row.id, True)
        return {"connector": _public(row), "oauth_url": None}

    meta = await discover_auth(url)
    if not meta.get("authorization_endpoint") or not meta.get("token_endpoint"):
        raise HTTPException(400, "This server did not advertise OAuth. Use bearer token auth instead.")
    client_id, client_secret = body.client_id, body.client_secret
    if not client_id and meta.get("registration_endpoint"):
        try:
            reg = await register_client(meta["registration_endpoint"])
            client_id, client_secret = reg.get("client_id"), reg.get("client_secret")
        except PermissionError as exc:
            raise HTTPException(400, str(exc))
    if not client_id:
        raise HTTPException(400, "This server needs a client_id. Paste one from the MCP host, or use a bearer token.")
    db.add(row)
    await db.flush()
    row.credentials_enc = encrypt(
        {"pending": True, "client_id": client_id, "client_secret": client_secret, "meta": meta}
    )
    await db.commit()
    return {
        "connector": _public(row),
        "start_url": f"/api/connectors/{row.id}/oauth/start",
        "oauth_url": None,
    }


async def _stash_oauth(payload: dict) -> str:
    state = secrets.token_urlsafe(24)
    await get_redis().set(f"mcp_oauth:{state}", json.dumps(payload).encode(), ex=600)
    return state


async def _load_oauth(state: str | None) -> dict | None:
    if not state:
        return None
    raw = await get_redis().get(f"mcp_oauth:{state}")
    if not raw:
        return None
    await get_redis().delete(f"mcp_oauth:{state}")
    return json.loads(raw)


@router.get("/{connector_id}/oauth/start")
async def start_mcp_oauth(connector_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    row = await db.get(Connector, connector_id)
    if row is None or row.user_id != user.id or row.kind != "mcp":
        raise HTTPException(404, "Not found")
    creds = decrypt_json(row.credentials_enc)
    meta = creds.get("meta") or await discover_auth(row.server_url or "")
    client_id, client_secret = creds.get("client_id"), creds.get("client_secret")
    if not client_id:
        raise HTTPException(400, "This connector has no OAuth client. Remove it and add it again.")
    verifier, challenge = pkce_pair()
    state = await _stash_oauth(
        {
            "verifier": verifier,
            "connector_id": str(row.id),
            "user_id": str(user.id),
            "meta": meta,
            "client_id": client_id,
            "client_secret": client_secret,
        }
    )
    url = authorize_url(meta, client_id, state, challenge)
    return RedirectResponse(url, status_code=302)


@router.get("/oauth/callback")
async def mcp_oauth_callback(
    request: Request,
    background: BackgroundTasks,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    web = get_settings().public_web_url.rstrip("/")
    if error:
        return RedirectResponse(f"{web}/chat?error={error}")
    saved = await _load_oauth(state)
    if not saved or not code:
        return RedirectResponse(f"{web}/chat?error=invalid_state")
    row = await db.get(Connector, uuid.UUID(saved["connector_id"]))
    if row is None:
        return RedirectResponse(f"{web}/chat?error=missing_connector")
    try:
        tokens = await exchange_mcp_code(saved["meta"], saved["client_id"], saved.get("client_secret"), code, saved["verifier"])
    except PermissionError as exc:
        log.warning("mcp token exchange failed: %s", exc)
        return RedirectResponse(f"{web}/chat?error=oauth_failed")
    packed = pack_oauth_creds(saved["meta"], tokens, saved["client_id"], saved.get("client_secret"))
    packed["meta"] = saved["meta"]
    packed["client_id"] = saved["client_id"]
    packed["client_secret"] = saved.get("client_secret")
    row.credentials_enc = encrypt(packed)
    row.health = "pending"
    await db.commit()
    background.add_task(sync_in_background, row.id, True)
    return RedirectResponse(f"{web}/chat?mcp=connected")


@router.patch("/{connector_id}")
async def patch_connector(
    connector_id: uuid.UUID,
    body: PatchConnector,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(Connector, connector_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(404, "Not found")
    if body.enabled is not None:
        row.enabled = body.enabled
    if body.alias is not None:
        row.alias = body.alias.strip() or None
    if body.description is not None:
        row.description = body.description.strip()
    if body.default_for is not None:
        row.default_for = body.default_for
    await db.commit()
    return {"connector": _public(row)}


@router.post("/{connector_id}/sync")
async def resync(connector_id: uuid.UUID, background: BackgroundTasks, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    row = await db.get(Connector, connector_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(404, "Not found")
    background.add_task(sync_in_background, row.id, True)
    return {"ok": True}


@router.delete("/{connector_id}")
async def remove(connector_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    row = await db.get(Connector, connector_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(404, "Not found")
    if row.kind == "builtin":
        raise HTTPException(400, "Sign-in mail connectors cannot be removed. Disable them instead.")
    await invalidate(connector_id=row.id)
    await db.delete(row)
    await db.commit()
    return {"ok": True}
