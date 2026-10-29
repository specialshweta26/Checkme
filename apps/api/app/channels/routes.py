"""WhatsApp webhooks (public, signed by the provider) and the signed-in linking API.

Public URLs (through the web app's /api proxy, which forwards the raw body and headers untouched):
  Twilio: {WHATSAPP_PUBLIC_BASE_URL or PUBLIC_WEB_URL}/api/channels/whatsapp/twilio         (POST, form-encoded)
          {...}/api/channels/whatsapp/twilio/status                                        (optional status callback)
  Meta:   {PUBLIC_WEB_URL}/api/channels/whatsapp/webhook                                   (GET verify + POST JSON)"""

from __future__ import annotations

import hmac
import json
import logging
import uuid
from urllib.parse import parse_qsl

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
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
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def _active(kind: str) -> bool:
    p = get_provider()
    return p.enabled() and getattr(p, "kind", "meta") == kind


async def _twilio_form(request: Request) -> list[tuple[str, str]]:
    """Validate X-Twilio-Signature over the public URL Twilio called (not the proxied internal one)."""
    p = get_provider()
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(413, "Payload too large")
    pairs = parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True)
    url = p.webhook_url(request.url.path, request.url.query)
    if not p.validate(url, pairs, request.headers.get("x-twilio-signature")):
        log.warning("twilio webhook: bad or missing signature")
        raise HTTPException(403, "Invalid signature")
    return pairs


@router.post("/twilio")
async def twilio_receive(request: Request, background: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """Twilio inbound message. Answers an empty TwiML 200 at once; the message is handled in the background and
    deduplicated on MessageSid (Twilio retries on timeouts)."""
    if not _active("twilio"):
        raise HTTPException(404, "WhatsApp (Twilio) is not configured")
    pairs = await _twilio_form(request)
    for msg in get_provider().parse(pairs):
        if await service.claim_message(db, msg.message_id):
            background.add_task(service.handle_inbound, msg)
    await db.commit()
    return Response(EMPTY_TWIML, media_type="application/xml")


@router.post("/twilio/status")
async def twilio_status(request: Request):
    """Optional delivery status callback; only failures are logged (status and error code, never the body)."""
    if not _active("twilio"):
        raise HTTPException(404, "WhatsApp (Twilio) is not configured")
    f = dict(await _twilio_form(request))
    status = f.get("MessageStatus") or ""
    if status in {"failed", "undelivered"}:
        log.warning("twilio message %s %s (error %s)", (f.get("MessageSid") or "")[:40], status, f.get("ErrorCode") or "-")
    return Response(status_code=204)


@router.get("/webhook")
async def verify(request: Request):
    """Meta's subscription check: echo hub.challenge when hub.verify_token matches."""
    if not _active("meta"):
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
    if not _active("meta"):
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
