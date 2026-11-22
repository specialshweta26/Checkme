"""Paid plans through Stripe (docs/BILLING.md): signed webhooks move a user to Pro and back, and Pro gets its own
daily cap. Stripe itself is never called: its REST calls are faked."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.billing import service

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
SECRET = "whsec_test"


def _sign(payload: bytes, secret: str = SECRET, t: int | None = None) -> str:
    t = int(time.time()) if t is None else t
    sig = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"


def test_signature_checks():
    body = b'{"type":"x"}'
    assert service.verify(body, _sign(body), SECRET)
    assert not service.verify(body + b" ", _sign(body), SECRET)  # changed body
    assert not service.verify(body, _sign(body, "whsec_other"), SECRET)  # another endpoint's secret
    assert not service.verify(body, _sign(body, t=int(time.time()) - 3600), SECRET)  # replayed an hour later
    assert not service.verify(body, "", SECRET) and not service.verify(body, _sign(body), "")


def test_form_encoding_matches_stripe():
    flat = dict(service._flat({"mode": "subscription", "line_items": [{"price": "price_1", "quantity": 1}],
                               "subscription_data": {"metadata": {"lumio_user": "u"}}, "allow_promotion_codes": True}))  # fmt: skip
    assert flat == {"mode": "subscription", "line_items[0][price]": "price_1", "line_items[0][quantity]": "1",
                    "subscription_data[metadata][lumio_user]": "u", "allow_promotion_codes": "true"}  # fmt: skip


pytestmark_db = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")


@pytest.fixture
async def env(monkeypatch):
    import httpx
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.agent import meter
    from app.billing import routes
    from app.config import get_settings
    from app.db import get_db
    from app.deps import current_user
    from app.models import Base, User

    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMPTZ"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    s = get_settings()
    for k, v in (("stripe_secret_key", "sk_test_x"), ("stripe_price_pro", "price_pro"), ("stripe_webhook_secret", SECRET),
                 ("daily_user_token_budget", 1000), ("plan_pro_token_budget", 0), ("budget_exempt", "")):  # fmt: skip
        monkeypatch.setattr(s, k, v)
    meter._cap_cache.clear()
    async with factory() as ses:
        user = User(email=f"payer-{uuid.uuid4().hex[:8]}@example.com", display_name="P", primary_provider="email")
        ses.add(user)
        await ses.commit()

    async def db():
        async with factory() as ses:
            yield ses

    calls: list[dict] = []

    async def fake_stripe(method, path, data=None, idempotency=""):
        calls.append({"method": method, "path": path, "data": data})
        return {"url": f"https://checkout.stripe.test{path}"}

    monkeypatch.setattr(service, "stripe", fake_stripe)

    async def spent(db, uid):
        return {"cost_usd": 0.0, "tokens": 5000, "limit_usd": None, "limit_tokens": None}

    monkeypatch.setattr(meter, "day_spend", spent)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    app.dependency_overrides[get_db] = db
    app.dependency_overrides[current_user] = lambda: user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield SimpleNamespace(c=c, user=user, calls=calls, factory=factory, meter=meter)
    async with factory() as ses:
        await ses.execute(text("DELETE FROM users WHERE id = :i"), {"i": user.id})
        await ses.commit()
    meter._cap_cache.clear()
    await engine.dispose()


async def _hook(c, event: dict):
    body = json.dumps(event).encode()
    return await c.post("/api/billing/webhook", content=body, headers={"stripe-signature": _sign(body)})


@pytestmark_db
async def test_checkout_then_webhooks_move_the_plan(env):
    c, user, meter = env.c, env.user, env.meter
    plan = (await c.get("/api/billing")).json()
    assert plan["enabled"] and plan["plan"] == "free" and plan["cap"]["tokens"] == 1000
    async with env.factory() as ses:
        assert await meter.over_user_cap(ses, user)  # 5000 tokens used against the free 1000
    r = await c.post("/api/billing/checkout")
    assert r.status_code == 200 and r.json()["url"].startswith("https://checkout.stripe.test")
    data = env.calls[0]["data"]
    assert data["client_reference_id"] == str(user.id) and data["customer_email"] == user.email
    assert data["line_items"] == [{"price": "price_pro", "quantity": 1}] and data["mode"] == "subscription"

    body = json.dumps({"type": "checkout.session.completed"}).encode()
    assert (await c.post("/api/billing/webhook", content=body, headers={"stripe-signature": "t=1,v1=bad"})).status_code == 400
    r = await _hook(c, {"type": "checkout.session.completed", "data": {"object": {
        "client_reference_id": str(user.id), "customer": "cus_1", "subscription": "sub_1", "payment_status": "paid"}}})
    assert r.status_code == 200
    r = await _hook(c, {"type": "customer.subscription.updated", "data": {"object": {
        "id": "sub_1", "customer": "cus_1", "status": "active", "current_period_end": int(time.time()) + 86400 * 30,
        "metadata": {"lumio_user": str(user.id)}}}})
    plan = (await c.get("/api/billing")).json()
    assert plan["plan"] == "pro" and plan["has_customer"] and plan["renews_at"] and plan["cap"]["tokens"] is None
    meter._cap_cache.clear()
    async with env.factory() as ses:
        assert not await meter.over_user_cap(ses, user)  # Pro has no daily cap here
    assert (await c.post("/api/billing/checkout")).status_code == 409
    r = await c.post("/api/billing/portal")
    assert r.status_code == 200 and env.calls[-1]["data"]["customer"] == "cus_1"

    r = await _hook(c, {"type": "customer.subscription.deleted", "data": {"object": {
        "id": "sub_1", "customer": "cus_1", "status": "canceled"}}})  # found by customer id alone
    plan = (await c.get("/api/billing")).json()
    assert plan["plan"] == "free" and plan["status"] == "canceled" and plan["cap"]["tokens"] == 1000


@pytestmark_db
async def test_events_for_strangers_change_nothing(env):
    r = await _hook(env.c, {"type": "checkout.session.completed", "data": {"object": {
        "client_reference_id": str(uuid.uuid4()), "customer": "cus_x", "payment_status": "paid"}}})
    assert r.status_code == 200
    r = await _hook(env.c, {"type": "customer.subscription.updated", "data": {"object": {"customer": "cus_nobody", "status": "active"}}})
    assert r.status_code == 200 and (await env.c.get("/api/billing")).json()["plan"] == "free"


async def test_off_without_stripe_keys(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "stripe_secret_key", "")
    assert not service.enabled()
