"""Paid plans through Stripe (docs/BILLING.md).

Stripe Checkout takes the payment and Stripe's customer portal handles cards, invoices and cancelling, so Lumio
never sees card details. Lumio keeps one ``subscriptions`` row per paying user, updated only from Stripe's signed
webhooks, and the Pro plan lifts the daily cap to PLAN_PRO_TOKEN_BUDGET / PLAN_PRO_BUDGET_USD.

Calls go straight to Stripe's REST API (form-encoded), so there's no SDK to keep up to date.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.config import get_settings

log = logging.getLogger(__name__)

API = "https://api.stripe.com/v1"
PAID = {"active", "trialing"}  # past_due keeps Pro while Stripe retries the card; see is_pro
GRACE = {"past_due"}
TOLERANCE_S = 300


def enabled() -> bool:
    s = get_settings()
    return bool(s.stripe_secret_key and s.stripe_price_pro)


def pro_cap() -> tuple[float, int]:
    s = get_settings()
    return float(s.plan_pro_budget_usd or 0), int(s.plan_pro_token_budget or 0)


async def row(db, user_id):
    from app.models import Subscription

    return await db.get(Subscription, user_id)


async def is_pro(db, user_id) -> bool:
    sub = await row(db, user_id)
    return bool(sub and sub.plan == "pro" and sub.status in PAID | GRACE)


def _flat(data: dict, prefix: str = "") -> list[tuple[str, str]]:
    """Stripe's form encoding: nested dicts and lists as a[b][0]=c."""
    out: list[tuple[str, str]] = []
    for k, v in data.items():
        key = f"{prefix}[{k}]" if prefix else str(k)
        if isinstance(v, dict):
            out += _flat(v, key)
        elif isinstance(v, list):
            for i, item in enumerate(v):
                out += _flat(item, f"{key}[{i}]") if isinstance(item, dict) else [(f"{key}[{i}]", str(item))]
        elif v is not None:
            out.append((key, "true" if v is True else "false" if v is False else str(v)))
    return out


async def stripe(method: str, path: str, data: dict | None = None, idempotency: str = "") -> dict:
    from connector_core.http import shared_client

    headers = {"Authorization": f"Bearer {get_settings().stripe_secret_key}"}
    if idempotency:
        headers["Idempotency-Key"] = idempotency
    resp = await shared_client().request(method, f"{API}{path}", data=_flat(data or {}), headers=headers, timeout=20)
    body = resp.json() if resp.content else {}
    if resp.status_code >= 400:
        msg = (body.get("error") or {}).get("message", "") if isinstance(body, dict) else ""
        log.warning("stripe %s %s failed: %s %s", method, path, resp.status_code, msg[:200])
        raise RuntimeError(msg or f"Stripe answered {resp.status_code}")
    return body


async def checkout_url(db, user) -> str:
    """A Stripe Checkout page for the Pro plan. The user id rides along so the webhook knows whose it is."""
    s = get_settings()
    sub = await row(db, user.id)
    data: dict[str, Any] = {
        "mode": "subscription",
        "line_items": [{"price": s.stripe_price_pro, "quantity": 1}],
        "success_url": f"{s.public_web_url}/billing?done=1",
        "cancel_url": f"{s.public_web_url}/billing",
        "client_reference_id": str(user.id),
        "subscription_data": {"metadata": {"lumio_user": str(user.id)}},
        "metadata": {"lumio_user": str(user.id)},
        "allow_promotion_codes": True,
    }
    if sub and sub.stripe_customer_id:
        data["customer"] = sub.stripe_customer_id
    else:
        data["customer_email"] = user.email
    out = await stripe("POST", "/checkout/sessions", data, idempotency=f"co-{user.id}-{int(time.time() // 60)}")
    return out["url"]


async def portal_url(db, user) -> str:
    sub = await row(db, user.id)
    if not sub or not sub.stripe_customer_id:
        raise LookupError("no Stripe customer yet")
    out = await stripe("POST", "/billing_portal/sessions",
                       {"customer": sub.stripe_customer_id, "return_url": f"{get_settings().public_web_url}/billing"})  # fmt: skip
    return out["url"]


def verify(payload: bytes, header: str, secret: str, now: float | None = None) -> bool:
    """Stripe-Signature: t=…,v1=… is an HMAC-SHA256 of "t.payload" with the endpoint's signing secret."""
    if not secret or not header:
        return False
    parts: dict[str, list[str]] = {}
    for item in header.split(","):
        k, _, v = item.strip().partition("=")
        parts.setdefault(k, []).append(v)
    try:
        t = int(parts.get("t", ["0"])[0])
    except ValueError:
        return False
    if abs((now or time.time()) - t) > TOLERANCE_S:
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, sig) for sig in parts.get("v1", []))


def _uid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


async def apply_event(db, event: dict) -> str:
    """Bring a user's plan in step with one webhook event. Returns what it did (for the log and tests)."""
    from app.models import Subscription, User

    kind = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    if kind == "checkout.session.completed":
        uid = _uid(obj.get("client_reference_id") or (obj.get("metadata") or {}).get("lumio_user"))
        if uid is None or await db.get(User, uid) is None:
            return "ignored: unknown user"
        sub = await db.get(Subscription, uid) or Subscription(user_id=uid)
        sub.stripe_customer_id = str(obj.get("customer") or sub.stripe_customer_id or "")
        sub.stripe_subscription_id = str(obj.get("subscription") or sub.stripe_subscription_id or "")
        if obj.get("payment_status") in ("paid", "no_payment_required"):
            sub.plan, sub.status = "pro", sub.status or "active"
        db.add(sub)
        return "checkout"
    if kind.startswith("customer.subscription."):
        uid = _uid((obj.get("metadata") or {}).get("lumio_user"))
        sub = await db.get(Subscription, uid) if uid else None
        if sub is None and obj.get("customer"):
            sub = (await db.execute(select(Subscription).where(Subscription.stripe_customer_id == str(obj["customer"])))).scalar_one_or_none()
        if sub is None:
            if uid is None or await db.get(User, uid) is None:
                return "ignored: unknown subscription"
            sub = Subscription(user_id=uid)
            db.add(sub)
        status = str(obj.get("status") or "")
        sub.status = "canceled" if kind.endswith(".deleted") else status
        sub.plan = "pro" if sub.status in PAID | GRACE else "free"
        sub.stripe_subscription_id = str(obj.get("id") or sub.stripe_subscription_id)
        sub.stripe_customer_id = str(obj.get("customer") or sub.stripe_customer_id)
        end = obj.get("current_period_end") or ((obj.get("items") or {}).get("data") or [{}])[0].get("current_period_end")
        sub.current_period_end = datetime.fromtimestamp(int(end), timezone.utc) if end else sub.current_period_end
        sub.cancel_at_period_end = bool(obj.get("cancel_at_period_end"))
        from app.agent import meter

        meter._cap_cache.pop(sub.user_id, None)
        return f"plan {sub.plan} ({sub.status})"
    return "ignored"
