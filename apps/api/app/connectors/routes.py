from __future__ import annotations

import asyncio
import json
import logging
import secrets
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
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
from app.connectors.platform import ensure_platform_connectors
from app.connectors.service import next_slug, sync_in_background
from app.connectors.urlsafety import UnsafeUrl, validate_public_url
from app.db import get_db
from app.deps import current_user
from app.models import Connector, ServerCard, User
from app.security import decrypt_json, encrypt, pkce_pair

log = logging.getLogger(__name__)
_screen_locks: dict[str, asyncio.Lock] = {}
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


@router.get("/marketplace")
async def marketplace(query: str = "", user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    from app.connectors.composio_box import list_connections, list_toolkits

    if not get_settings().composio_api_key:
        return {"apps": [], "connections": [], "configured": False}
    await ensure_platform_connectors(db, user)
    fetched = await asyncio.gather(list_toolkits(query, 2000), list_connections(str(user.id)), return_exceptions=True)
    apps = fetched[0] if isinstance(fetched[0], list) else []
    connections = fetched[1] if isinstance(fetched[1], list) else []
    if isinstance(fetched[0], Exception):
        log.warning("composio toolkit list failed: %s", fetched[0])
        raise HTTPException(502, "Could not load the Composio catalog. Try again.")
    if isinstance(fetched[1], Exception):
        log.warning("composio connection list failed: %s", fetched[1])
    active: dict[str, str] = {}
    for row in connections:
        slug = str(row.get("toolkit") or "")
        status = str(row.get("status") or "").upper()
        if slug and status in {"ACTIVE", "SUCCESS", "ENABLED", "CONNECTED"}:
            active[slug] = str(row.get("id") or "")
    for app in apps:
        app["connected"] = app.get("slug") in active
        app["connection_id"] = active.get(app.get("slug") or "") or None
    return {"apps": apps, "connections": connections, "configured": True}


class ConnectAppBody(BaseModel):
    toolkit: str
    agent_id: str | None = None
    agent_ids: list[str] | None = None
    credentials: dict[str, str] | None = None


@router.post("/marketplace/connect")
async def marketplace_connect(body: ConnectAppBody, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    from app.connectors.composio_box import start_connect

    if not get_settings().composio_api_key:
        raise HTTPException(400, "COMPOSIO_API_KEY is not configured")
    await ensure_platform_connectors(db, user)
    toolkit = body.toolkit.strip().lower()
    callback = f"{get_settings().public_web_url.rstrip('/')}/connectors?composio=connected"
    try:
        data = await start_connect(str(user.id), toolkit, callback, body.credentials)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)[:300])
    if data.get("needs_credentials"):
        return data
    if data.get("connected"):
        pass
    elif not data.get("redirect_url"):
        raise HTTPException(400, "Composio did not return a sign-in URL. This app may need an API key instead of OAuth.")
    assignee_ids = list(dict.fromkeys([*(body.agent_ids or []), *([body.agent_id] if body.agent_id else [])]))
    from app.agents import service as agents_service

    try:
        if assignee_ids:
            for aid in assignee_ids:
                await agents_service.grant_toolkit_agent(db, user, toolkit, aid)
        else:
            await agents_service.set_toolkit_agents(db, user, toolkit, [])
            composio = (
                await db.execute(select(Connector).where(Connector.user_id == user.id, Connector.slug == "composio"))
            ).scalar_one_or_none()
            if composio is not None:
                await agents_service.set_connector_agents(db, user, composio.id, [])
    except agents_service.AgentError:
        pass
    return {
        **data,
        "toolkit": toolkit,
        "name": data.get("name") or toolkit,
        "logo": data.get("logo"),
        "description": (data.get("description") or "")[:240],
    }


@router.delete("/marketplace/connections/{account_id}")
async def marketplace_disconnect(account_id: str, user: User = Depends(current_user)):
    from app.connectors.composio_box import disconnect_account

    if not get_settings().composio_api_key:
        raise HTTPException(400, "COMPOSIO_API_KEY is not configured")
    try:
        return await disconnect_account(account_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)[:300]) from exc


async def _computer_row(db: AsyncSession, user: User, provision: bool = True):
    if provision:
        await ensure_platform_connectors(db, user)
    row = (
        await db.execute(select(Connector).where(Connector.user_id == user.id, Connector.slug == "computer"))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "No computer")
    return row


@router.get("/computer/screen")
async def computer_screen(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Capture the current Chromium frame, using a user-scoped cache on capture failure."""
    from app.connectors.daytona_box import call_daytona, live_screen_png

    row = await _computer_row(db, user, provision=False)
    user_key = str(user.id)
    lock = _screen_locks.setdefault(user_key, asyncio.Lock())
    if lock.locked():
        cached = live_screen_png(user_key)
        if cached:
            return Response(content=cached, media_type="image/png", headers={"Cache-Control": "no-store", "X-Screen-Stale": "true"})
        raise HTTPException(404, "No live screen yet")
    async with lock:
        try:
            data = await asyncio.wait_for(call_daytona(row, "screen_png", {}), timeout=10)
        except Exception as vis_exc:  # noqa: BLE001
            cached = live_screen_png(user_key)
            if cached:
                return Response(content=cached, media_type="image/png", headers={"Cache-Control": "no-store", "X-Screen-Stale": "true"})
            raise HTTPException(404, str(vis_exc)[:200]) from vis_exc
        if not isinstance(data, (bytes, bytearray)) or len(data) < 32 or not bytes(data).startswith(b"\x89PNG"):
            cached = live_screen_png(user_key)
            if cached:
                return Response(content=cached, media_type="image/png", headers={"Cache-Control": "no-store", "X-Screen-Stale": "true"})
            raise HTTPException(404, "No live screen yet")
        return Response(content=bytes(data), media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/computer/status")
async def computer_status(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Whether the live computer is stopped, asleep, or running."""
    backend = get_settings().computer_backend
    stopped = {"state": "stopped", "screenAvailable": False, "url": "", "title": "", "screen_agent": "", "provider": backend or "none"}
    if not backend:
        return {**stopped, "error": "No computer is configured on the server (set E2B_API_KEY)."}
    try:
        row = await _computer_row(db, user, provision=False)
    except HTTPException:
        return stopped
    from app.connectors.daytona_box import call_daytona

    try:
        data = await call_daytona(row, "status", {})
    except Exception as exc:  # noqa: BLE001
        log.warning("computer status failed (%s): %s: %s", backend, type(exc).__name__, str(exc)[:200])
        return {**stopped, "error": str(exc)[:240]}
    if not isinstance(data, dict):
        return stopped
    return {
        "state": data.get("state") or "stopped",
        "screenAvailable": bool(data.get("screenAvailable")),
        "url": data.get("url") or "",
        "title": data.get("title") or "",
        "screen_agent": data.get("screen_agent") or "",
        **({"error": str(data["error"])[:300]} if data.get("error") else {}),
        **({"booting_for": data["booting_for"]} if data.get("booting_for") else {}),
        **_stream_fields(data),
    }


BOOT_TIMEOUT = 90.0


def _stream_fields(data: dict) -> dict:
    """Live desktop stream (E2B noVNC). Only for the signed-in owner: the URLs carry the stream password."""
    out = {"provider": data.get("provider") or "daytona"}
    for key in ("stream_url", "control_url"):
        url = str(data.get(key) or "")
        if url.startswith("https://"):
            out[key] = url
    return out


@router.post("/computer/boot")
async def computer_boot(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Wake the computer (E2B desktop or Daytona) and its Chromium so the right-rail screen can stream."""
    row = await _computer_row(db, user, provision=True)
    from app.connectors.daytona_box import call_daytona

    backend = get_settings().computer_backend
    if not backend:
        raise HTTPException(503, "No computer is configured on the server (set E2B_API_KEY).")
    log.info("computer boot requested user=%s provider=%s", user.id, backend)
    try:
        # E2B work runs in a thread, so a timeout here only stops waiting; a slow boot still finishes and
        # the status poll picks the stream up.
        data = await asyncio.wait_for(asyncio.shield(call_daytona(row, "boot", {})), timeout=BOOT_TIMEOUT)
    except asyncio.TimeoutError as vis_exc:
        log.warning("computer boot timed out after %.0fs user=%s provider=%s", BOOT_TIMEOUT, user.id, backend)
        raise HTTPException(
            504, f"The computer did not start within {int(BOOT_TIMEOUT)} seconds. It may still come up; check again shortly."
        ) from vis_exc
    except Exception as vis_exc:  # noqa: BLE001
        log.warning("computer boot failed user=%s provider=%s: %s", user.id, backend, str(vis_exc)[:240])
        raise HTTPException(400, str(vis_exc)[:240]) from vis_exc
    await db.commit()  # keeps a newly created sandbox id
    if not isinstance(data, dict):
        return {"state": "running", "screenAvailable": True, "url": "", "title": ""}
    return {
        "ok": data.get("ok", True),
        "state": data.get("state") or "running",
        "screenAvailable": True,
        "url": data.get("url") or "",
        "title": data.get("title") or "Computer",
        "error": data.get("error") or "",
        **_stream_fields(data),
    }


class ComputerControlBody(BaseModel):
    kind: str
    x: int | None = None
    y: int | None = None
    text: str | None = None
    key: str | None = None
    direction: str | None = None
    amount: int | None = None
    dx: int | None = None
    dy: int | None = None
    ms: int | None = None


@router.post("/computer/control")
async def computer_control(body: ComputerControlBody, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """User takeover: click, type, key, and scroll on the same live computer screen."""
    kind = (body.kind or "").strip().lower()
    if kind not in {"click", "type", "fill", "key", "press", "scroll", "wait", "move"}:
        raise HTTPException(400, "unsupported action")
    row = await _computer_row(db, user)
    from app.connectors.daytona_box import call_daytona

    step = {
        "kind": "key" if kind == "press" else kind,
        "x": body.x,
        "y": body.y,
        "text": body.text,
        "key": body.key or body.text,
        "direction": body.direction,
        "amount": body.amount,
        "dx": body.dx,
        "dy": body.dy,
        "ms": body.ms,
    }
    try:
        data = await call_daytona(row, "computer_act", {"actions": [step]})
    except Exception as vis_exc:  # noqa: BLE001
        raise HTTPException(400, str(vis_exc)[:240]) from vis_exc
    if not isinstance(data, dict):
        return {"ok": True}
    return {
        "ok": not data.get("error"),
        "url": data.get("url") or "",
        "title": data.get("title") or "",
        "error": data.get("error") or "",
    }


@router.get("")
async def list_connectors(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    await ensure_platform_connectors(db, user)
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
    if row.slug == "computer":
        raise HTTPException(400, "The computer cannot be removed.")
    if row.kind == "builtin":
        row.enabled = False
        await db.commit()
        return {"ok": True, "disabled": True}
    await invalidate(connector_id=row.id)
    await db.delete(row)
    await db.commit()
    return {"ok": True}
