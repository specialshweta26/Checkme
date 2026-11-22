"""Billing API (docs/BILLING.md): the plan page, Stripe Checkout and portal links, and Stripe's webhook."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing import service
from app.config import get_settings
from app.db import get_db
from app.deps import current_user
from app.models import User

log = logging.getLogger(__name__)
router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("")
async def plan(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    from app.agent import meter

    sub = await service.row(db, user.id) if service.enabled() else None
    pro = bool(sub and await service.is_pro(db, user.id))
    usd, tokens = await meter.cap_for(db, user)
    spend = await meter.day_spend(db, user.id)
    free_usd, free_tokens = meter.user_cap()
    pro_usd, pro_tokens = service.pro_cap()
    return {
        "enabled": service.enabled(),
        "plan": "pro" if pro else "free",
        "status": sub.status if sub else "",
        "renews_at": sub.current_period_end.isoformat() if sub and sub.current_period_end else None,
        "cancel_at_period_end": bool(sub and sub.cancel_at_period_end),
        "has_customer": bool(sub and sub.stripe_customer_id),
        "today": {"tokens": spend["tokens"], "cost_usd": spend["cost_usd"]},
        "cap": {"tokens": tokens or None, "usd": usd or None},
        "plans": {"free": {"tokens": free_tokens or None, "usd": free_usd or None},
                  "pro": {"tokens": pro_tokens or None, "usd": pro_usd or None}},
        "exempt": meter.cap_exempt(user.email),
    }  # fmt: skip


@router.post("/checkout")
async def checkout(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if not service.enabled():
        raise HTTPException(404, "Paid plans aren't available on this server.")
    if await service.is_pro(db, user.id):
        raise HTTPException(409, "You're already on Pro. Manage it from Billing.")
    try:
        return {"url": await service.checkout_url(db, user)}
    except Exception:  # noqa: BLE001
        raise HTTPException(502, "Couldn't open the payment page. Try again in a minute.")


@router.post("/portal")
async def portal(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if not service.enabled():
        raise HTTPException(404, "Paid plans aren't available on this server.")
    try:
        return {"url": await service.portal_url(db, user)}
    except LookupError:
        raise HTTPException(400, "There's no subscription to manage yet.")
    except Exception:  # noqa: BLE001
        raise HTTPException(502, "Couldn't open billing. Try again in a minute.")


@router.post("/webhook")
async def webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Stripe's events. Only signed ones count; anything else gets 400 and changes nothing."""
    payload = await request.body()
    if not service.verify(payload, request.headers.get("stripe-signature", ""), get_settings().stripe_webhook_secret):
        raise HTTPException(400, "bad signature")
    try:
        event = json.loads(payload)
    except ValueError:
        raise HTTPException(400, "bad payload")
    what = await service.apply_event(db, event)
    if not what.startswith("ignored"):
        from app.admin import audit

        sub_user = None
        obj = (event.get("data") or {}).get("object") or {}
        sub_user = service._uid(obj.get("client_reference_id") or (obj.get("metadata") or {}).get("lumio_user"))
        audit.record(db, "billing", sub_user, None, target=event.get("type", ""), detail={"result": what})
    await db.commit()
    log.info("stripe %s: %s", event.get("type"), what)
    return {"ok": True}
