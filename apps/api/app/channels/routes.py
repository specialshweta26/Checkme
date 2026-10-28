"""WhatsApp webhook (public, signed by Meta) and the signed-in linking API.

Public URL (through the web app's /api proxy, which forwards the raw body and headers):
  {PUBLIC_WEB_URL}/api/channels/whatsapp/webhook"""

from __future__ import annotations

import hmac
import json
import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels import service
from app.channels.provider import get_provider
from app.config import get_settings
from app.db import get_db
from app.deps import current_user
from app.models import User

log = logging.getLogger(__name__)

router = APIRouter(prefix="/channels/whatsapp", tags=["channels"])
MAX_BODY = 1024 * 1024


@router.get("/webhook")
async def verify(request: Request):
    """Meta's subscription check: echo hub.challenge when hub.verify_token matches."""
    if not service.enabled():
        raise HTTPException(404, "WhatsApp is not configured")
    q = request.query_params
    token = q.get("hub.verify_token") or ""
    expected = get_settings().whatsapp_verify_token
    if q.get("hub.mode") == "subscribe" and expected and hmac.compare_digest(token.encode(), expected.encode()):
        return PlainTextResponse(q.get("hub.challenge") or "")
    raise HTTPException(403, "Verification failed")


@router.post("/webhook")
async def receive(request: Request, background: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """Signature-checked over the raw body, deduplicated by message id, handled after the 200 goes back."""
    if not service.enabled():
        raise HTTPException(404, "WhatsApp is not configured")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(413, "Payload too large")
    provider = get_provider()
    if not provider.verify_signature(raw, request.headers.get("x-hub-signature-256")):
        log.warning("whatsapp webhook: bad or missing signature")
        raise HTTPException(401, "Invalid signature")
    try:
        payload = json.loads(raw or b"{}")
    except ValueError as exc:
        raise HTTPException(400, "Invalid JSON") from exc
    accepted = 0
    for msg in provider.parse(payload):
        if await service.claim_message(db, msg.message_id):
            accepted += 1
            background.add_task(service.handle_inbound, msg)
    await db.commit()
    return {"ok": True, "accepted": accepted}


@router.get("")
async def whatsapp_status(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await service.status(db, user)


@router.post("/link-code")
async def link_code(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if not service.enabled():
        raise HTTPException(400, "WhatsApp isn't set up on this server yet (see docs/WHATSAPP.md).")
    if await service._limited(f"wa-code:{user.id}", 10, 600):
        raise HTTPException(429, "Too many link codes. Wait a few minutes.")
    out = await service.create_link_code(db, user)
    await db.commit()
    return out


@router.delete("/links/{link_id}")
async def unlink(link_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if not await service.unlink(db, user, link_id):
        raise HTTPException(404, "Unknown link")
    await db.commit()
    return {"ok": True}
