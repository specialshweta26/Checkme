"""The admin console (docs/ADMIN.md): invites let people sign up, suspension locks an account out, and the audit log
records it all. Against Postgres."""

from __future__ import annotations

import os
import re
import uuid

import pytest
from sqlalchemy import text

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")

PW = "correct horse battery"


@pytest.fixture
async def env(monkeypatch):
    import httpx
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.cache as cache
    from app.admin import routes as admin_routes
    from app.auth import email as email_routes
    from app.auth import mailer, routes
    from app.config import get_settings
    from app.db import get_db
    from app.models import Base

    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMPTZ"))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def db():
        async with factory() as s:
            yield s

    sent: list[dict] = []

    async def send(to, subject, body, page):
        sent.append({"to": to, "subject": subject, "text": body})
        return True

    async def never_limited(key, limit, window_s):
        return False

    def no_redis():
        raise RuntimeError("no redis in this test")

    monkeypatch.setattr(cache, "get_redis", no_redis)
    monkeypatch.setattr(mailer, "send", send)
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(email_routes, "_limited", never_limited)
    s = get_settings()
    tag = uuid.uuid4().hex[:8]
    admin_email = f"boss-{tag}@example.com"
    monkeypatch.setattr(s, "admin_emails", admin_email)
    monkeypatch.setattr(s, "signup_mode", "open")
    monkeypatch.setattr(s, "signup_allow", "")
    app = FastAPI()
    for r in (email_routes.router, routes.router, routes.me_router, admin_routes.router, admin_routes.me_router):
        app.include_router(r, prefix="/api")
    app.dependency_overrides[get_db] = db
    emails = [admin_email]
    domain = f"team-{tag}.org"

    async def account(c, email):
        emails.append(email)
        r = await c.post("/api/auth/email/signup", json={"email": email, "password": PW, "name": "T"})
        assert r.status_code == 200, r.text
        token = re.search(r"/verify-email\?token=(\S+)", sent[-1]["text"]).group(1)
        r = await c.post("/api/auth/email/verify", json={"token": token})
        assert r.status_code == 200, r.text
        return r

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as admin, \
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as other:  # fmt: skip
        await account(admin, admin_email)
        yield {"admin": admin, "other": other, "account": account, "s": s, "monkeypatch": monkeypatch, "sent": sent,
               "domain": domain, "emails": emails, "factory": factory}  # fmt: skip
    async with factory() as ses:
        for e in emails:
            await ses.execute(text("DELETE FROM users WHERE email = :e"), {"e": e})
            await ses.execute(text("DELETE FROM password_logins WHERE email = :e"), {"e": e})
        await ses.execute(text("DELETE FROM signup_invites WHERE value LIKE :d"), {"d": f"%{domain}"})
        await ses.commit()
    await engine.dispose()


async def test_only_admins_see_the_console(env):
    other = env["other"]
    await env["account"](other, f"someone-{uuid.uuid4().hex[:6]}@example.com")
    assert (await other.get("/api/admin/overview")).status_code == 404
    assert (await other.get("/api/me")).json()["admin"] is False
    assert (await env["admin"].get("/api/me")).json()["admin"] is True
    ov = (await env["admin"].get("/api/admin/overview")).json()
    assert ov["users"] >= 2 and ov["signup_mode"] == "open"


async def test_an_invite_lets_someone_sign_up_while_invite_only(env):
    admin, other, s, mp = env["admin"], env["other"], env["s"], env["monkeypatch"]
    mp.setattr(s, "signup_mode", "invite")
    newbie = f"newbie@{env['domain']}"
    env["emails"].append(newbie)
    r = await other.post("/api/auth/email/signup", json={"email": newbie, "password": PW})
    assert r.status_code == 403
    r = await admin.post("/api/admin/invites", json={"value": env["domain"], "send": False})
    assert r.status_code == 200 and r.json()["value"] == env["domain"]
    assert (await admin.post("/api/admin/invites", json={"value": "not an address"})).status_code == 400
    await env["account"](other, newbie)  # the domain invite lets them in
    r = await admin.post("/api/admin/invites", json={"value": f"pal@{env['domain']}", "send": True})
    assert r.json()["sent"] is True and env["sent"][-1]["subject"] == "You're invited to Lumio"
    inv = (await admin.get("/api/admin/invites")).json()
    assert {i["value"] for i in inv} >= {env["domain"], f"pal@{env['domain']}"}
    for i in inv:
        if i["value"].endswith(env["domain"]):
            assert (await admin.delete(f"/api/admin/invites/{i['id']}")).status_code == 200


async def test_suspend_locks_out_and_restore_lets_back_in(env):
    admin, other = env["admin"], env["other"]
    email = f"suspect-{uuid.uuid4().hex[:6]}@example.com"
    await env["account"](other, email)
    assert (await other.get("/api/me")).status_code == 200
    people = (await admin.get(f"/api/admin/users?q={email}")).json()
    assert len(people) == 1 and people[0]["email"] == email and people[0]["last_sign_in"]
    uid = people[0]["id"]
    me_id = (await admin.get("/api/me")).json()["user_id"]
    assert (await admin.post(f"/api/admin/users/{me_id}/suspend", json={"suspended": True})).status_code == 400
    assert (await admin.post(f"/api/admin/users/{uid}/suspend", json={"suspended": True})).status_code == 200
    r = await other.get("/api/me")
    assert r.status_code in (401, 403)
    other.cookies.clear()
    r = await other.post("/api/auth/email/login", json={"email": email, "password": PW})
    assert r.status_code == 403 and "suspended" in r.json()["detail"]
    from app.agent import meter
    from app.models import User

    async with env["factory"]() as ses:
        assert await meter.over_user_cap(ses, await ses.get(User, uuid.UUID(uid)))  # tasks and background stop too
    assert (await admin.post(f"/api/admin/users/{uid}/suspend", json={"suspended": False})).status_code == 200
    assert (await other.post("/api/auth/email/login", json={"email": email, "password": PW})).status_code == 200
    log = (await admin.get("/api/admin/audit?days=1")).json()
    actions = [(a["action"], a["target"]) for a in log]
    assert ("user_suspended", email) in actions and ("user_restored", email) in actions
    assert any(a["action"] == "sign_in" and a["user"] == email for a in log)
    csv = await admin.get("/api/admin/audit?days=1&fmt=csv")
    assert csv.status_code == 200 and csv.text.startswith("at,action,user") and "user_suspended" in csv.text
    mine = (await other.get("/api/me/audit")).json()
    assert {"sign_up", "sign_in", "user_suspended"} <= {a["action"] for a in mine}


def test_csv_export_quotes_formula_cells():
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from app.admin import audit

    row = SimpleNamespace(at=datetime.now(timezone.utc), action="invite_added", user_id=None, actor_id=None,
                          target="=HYPERLINK(\"x\")", detail={}, ip="")  # fmt: skip
    assert "'=HYPERLINK" in audit.to_csv([row], {})
