"""Email and password sign-in, password reset and enterprise SSO (docs/AUTH.md), end to end against Postgres."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")

from app.security import SESSION_COOKIE  # noqa: E402

PW = "correct horse battery"


@pytest.fixture
async def client(monkeypatch):
    import httpx
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.auth import email as email_routes
    from app.auth import mailer, routes, sso
    from app.config import get_settings
    from app.db import get_db
    from app.models import Base

    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
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

    monkeypatch.setattr(mailer, "send", send)
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(email_routes, "_limited", never_limited)
    monkeypatch.setattr(get_settings(), "sso_connections", "")

    app = FastAPI()
    for r in (email_routes.router, sso.router, routes.router, routes.me_router):
        app.include_router(r, prefix="/api")
    app.dependency_overrides[get_db] = db
    emails: list[str] = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        c.sent, c.emails, c.factory = sent, emails, factory
        yield c
    async with factory() as s:
        for e in emails:
            await s.execute(text("DELETE FROM users WHERE id IN (SELECT user_id FROM password_logins WHERE email = :e) OR email = :e"), {"e": e})
            await s.execute(text("DELETE FROM password_logins WHERE email = :e"), {"e": e})
        await s.commit()
    await engine.dispose()


def _email(c, prefix="a") -> str:
    e = f"{prefix}-{uuid.uuid4().hex[:10]}@example.com"
    c.emails.append(e)
    return e


def _token(c, path: str) -> str:
    m = re.search(rf"{path}\?token=(\S+)", c.sent[-1]["text"])
    assert m, c.sent[-1]
    return m.group(1)


async def _signup(c, email: str, password: str = PW) -> str:
    r = await c.post("/api/auth/email/signup", json={"email": email, "password": password, "name": "Asha Rao"})
    assert r.status_code == 200, r.text
    return _token(c, "/verify-email")


async def test_sign_up_verify_sign_in_and_sign_out(client):
    c = client
    email = _email(c)
    token = await _signup(c, email.upper())  # addresses are case-insensitive
    r = await c.post("/api/auth/email/login", json={"email": email, "password": PW})
    assert r.status_code == 403 and "Confirm your email" in r.json()["detail"]  # not before verifying
    r = await c.post("/api/auth/email/verify", json={"token": token})
    assert r.status_code == 200 and r.json()["redirect"] == "/chat" and r.headers.get("set-cookie", "").startswith(SESSION_COOKIE + "=")
    me = (await c.get("/api/me")).json()
    assert me["email"] == email and me["name"] == "Asha Rao" and me["primary_provider"] == "email"
    c.cookies.clear()
    assert (await c.post("/api/auth/email/login", json={"email": email, "password": "wrong password here"})).status_code == 401
    r = await c.post("/api/auth/email/login", json={"email": email, "password": PW})
    assert r.status_code == 200 and (await c.get("/api/me")).json()["email"] == email


async def test_sign_up_never_says_whether_an_account_exists(client):
    c = client
    email = _email(c)
    await c.post("/api/auth/email/verify", json={"token": await _signup(c, email)})
    first = await c.post("/api/auth/email/signup", json={"email": email, "password": "another long password"})
    other = await c.post("/api/auth/email/signup", json={"email": _email(c), "password": "another long password"})
    assert first.json() == other.json()
    assert c.sent[-2]["subject"] == "You already have a Lumio account"  # the owner is told, the asker isn't
    c.cookies.clear()
    assert (await c.post("/api/auth/email/login", json={"email": email, "password": "another long password"})).status_code == 401


async def test_someone_who_registers_your_address_first_gains_nothing(client):
    c = client
    email = _email(c)
    attacker_link = await _signup(c, email, "attacker chosen pass")
    owner_link = await _signup(c, email, PW)
    assert (await c.post("/api/auth/email/verify", json={"token": attacker_link})).status_code == 400  # superseded
    assert (await c.post("/api/auth/email/verify", json={"token": owner_link})).status_code == 200
    c.cookies.clear()
    assert (await c.post("/api/auth/email/login", json={"email": email, "password": "attacker chosen pass"})).status_code == 401


async def test_weak_passwords_and_bad_addresses_are_refused(client):
    c = client
    for pw in ("short", "password123", "1234567890123", "aaaaaaaaaaaa"):
        r = await c.post("/api/auth/email/signup", json={"email": _email(c), "password": pw})
        assert r.status_code == 400, pw
    assert (await c.post("/api/auth/email/signup", json={"email": "not-an-email", "password": PW})).status_code == 400


async def test_forgot_and_reset_password_signs_out_other_sessions(client):
    c = client
    email = _email(c)
    await c.post("/api/auth/email/verify", json={"token": await _signup(c, email)})
    old_cookies = dict(c.cookies)
    r = await c.post("/api/auth/email/forgot", json={"email": email})
    unknown = await c.post("/api/auth/email/forgot", json={"email": _email(c)})
    assert r.json() == unknown.json()  # same answer for an unknown address
    link = _token(c, "/reset-password")
    assert (await c.post("/api/auth/email/reset", json={"token": link, "password": "short"})).status_code == 400
    c.cookies.clear()
    r = await c.post("/api/auth/email/reset", json={"token": link, "password": "a brand new passphrase"})
    assert r.status_code == 200 and (await c.get("/api/me")).status_code == 200
    assert c.sent[-1]["subject"] == "Your Lumio password was changed"
    assert (await c.post("/api/auth/email/reset", json={"token": link, "password": "yet another passphrase"})).status_code == 400  # once
    c.cookies.clear()
    c.cookies.update(old_cookies)
    assert (await c.get("/api/me")).status_code == 401  # the session from before the reset is signed out
    c.cookies.clear()
    assert (await c.post("/api/auth/email/login", json={"email": email, "password": PW})).status_code == 401
    assert (await c.post("/api/auth/email/login", json={"email": email, "password": "a brand new passphrase"})).status_code == 200


async def test_forgot_tells_a_google_user_how_they_sign_in(client):
    from app.models import Account, User

    c = client
    email = _email(c, "g")
    async with c.factory() as s:
        u = User(email=email, display_name="G", primary_provider="google")
        s.add(u)
        await s.flush()
        s.add(Account(user_id=u.id, provider="google", subject=uuid.uuid4().hex, email=email))
        await s.commit()
    await c.post("/api/auth/email/forgot", json={"email": email})
    assert c.sent[-1]["subject"] == "How you sign in to Lumio" and "Google" in c.sent[-1]["text"]


async def test_eight_wrong_passwords_lock_the_address(client):
    c = client
    email = _email(c)
    await c.post("/api/auth/email/verify", json={"token": await _signup(c, email)})
    c.cookies.clear()
    for _ in range(8):
        assert (await c.post("/api/auth/email/login", json={"email": email, "password": "wrong password here"})).status_code == 401
    r = await c.post("/api/auth/email/login", json={"email": email, "password": PW})
    assert r.status_code == 429  # even the right password waits


async def test_change_password_keeps_this_session(client):
    c = client
    email = _email(c)
    await c.post("/api/auth/email/verify", json={"token": await _signup(c, email)})
    r = await c.post("/api/auth/email/password", json={"current": "wrong one entirely", "password": "a brand new passphrase"})
    assert r.status_code == 401
    r = await c.post("/api/auth/email/password", json={"current": PW, "password": "a brand new passphrase"})
    assert r.status_code == 200 and (await c.get("/api/me")).status_code == 200


# --- SSO ---------------------------------------------------------------------------------------------------------


def _rsa():
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    import jwt

    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": "k1", "alg": "RS256", "use": "sig"})
    return key, jwk


@pytest.fixture
def idp(monkeypatch):
    """A fake OIDC provider at https://idp.example.com for the acme.com domain."""
    import jwt

    from app.auth import sso
    from app.config import get_settings

    key, jwk = _rsa()
    conf = [{"id": "acme", "name": "Acme", "issuer": "https://idp.example.com", "client_id": "lumio-client",
             "client_secret": "s", "domains": ["acme.com"]}]  # fmt: skip
    monkeypatch.setattr(get_settings(), "sso_connections", json.dumps(conf))
    sso._cache.clear()
    doc = {"issuer": "https://idp.example.com", "authorization_endpoint": "https://idp.example.com/authorize",
           "token_endpoint": "https://idp.example.com/token", "jwks_uri": "https://idp.example.com/jwks"}  # fmt: skip
    state = SimpleNamespace(claims={}, aud="lumio-client", key=key)

    async def get_json(url):
        return doc if url.endswith("openid-configuration") else {"keys": [jwk]}

    monkeypatch.setattr(sso, "_get_json", get_json)

    class Client:
        async def post(self, url, data=None, timeout=None):
            now = int(time.time())
            claims = {"iss": "https://idp.example.com", "aud": state.aud, "iat": now, "exp": now + 300, **state.claims}
            tok = jwt.encode(claims, state.key, algorithm="RS256", headers={"kid": "k1"})
            return SimpleNamespace(status_code=200, json=lambda: {"id_token": tok, "access_token": "a"}, text="")

    import connector_core.http as http

    monkeypatch.setattr(http, "shared_client", lambda: Client())
    return state


async def _sso_round(c, idp, claims: dict, email_hint: str = ""):
    from urllib.parse import parse_qs, urlparse

    r = await c.get(f"/api/auth/sso/acme/login?email={email_hint}")
    assert r.status_code == 307, r.text
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["redirect_uri"][0].endswith("/api/auth/sso/acme/callback")
    idp.claims = {"nonce": q["nonce"][0], **claims}
    return await c.get(f"/api/auth/sso/acme/callback?code=c1&state={q['state'][0]}")


async def test_sso_discovers_by_domain_and_signs_in(client, idp):
    c = client
    assert (await c.get("/api/auth/sso/discover?email=ravi@acme.com")).json() == {"sso": True, "id": "acme", "name": "Acme", "enforce": False}
    assert (await c.get("/api/auth/sso/discover?email=ravi@other.com")).json() == {"sso": False}
    email = f"ravi-{uuid.uuid4().hex[:6]}@acme.com"
    c.emails.append(email)
    sub = uuid.uuid4().hex
    r = await _sso_round(c, idp, {"sub": sub, "email": email, "email_verified": True, "name": "Ravi K"}, email)
    assert r.status_code == 307 and r.headers["location"].endswith("/chat")
    me = (await c.get("/api/me")).json()
    assert me["email"] == email and me["primary_provider"] == "sso" and me["name"] == "Ravi K"
    c.cookies.clear()
    r = await _sso_round(c, idp, {"sub": sub, "email": email})  # the same person again: same user
    assert (await c.get("/api/me")).json()["user_id"] == me["user_id"]


async def test_sso_refuses_bad_tokens_and_other_domains(client, idp):
    from cryptography.hazmat.primitives.asymmetric import rsa

    c = client
    r = await _sso_round(c, idp, {"sub": "x1", "email": "eve@evil.com"})
    assert "error=sso_wrong_domain" in r.headers["location"]
    r = await _sso_round(c, idp, {"sub": "x2", "email": "eve@acme.com", "email_verified": False})
    assert "error=sso_unverified_email" in r.headers["location"]
    idp.aud = "someone-else"
    r = await _sso_round(c, idp, {"sub": "x3", "email": "eve@acme.com"})
    assert "error=sso_failed" in r.headers["location"]
    idp.aud, idp.key = "lumio-client", rsa.generate_private_key(public_exponent=65537, key_size=2048)  # forged signature
    r = await _sso_round(c, idp, {"sub": "x4", "email": "eve@acme.com"})
    assert "error=sso_failed" in r.headers["location"]
    r = await c.get("/api/auth/sso/acme/callback?code=c1&state=forged")
    assert "error=invalid_state" in r.headers["location"]
    assert (await c.get("/api/me")).status_code == 401


async def test_an_enforced_sso_domain_can_not_use_a_password(client, monkeypatch):
    from app.config import get_settings

    conf = [{"id": "acme", "issuer": "https://idp.example.com", "client_id": "x", "domains": ["acme.com"], "enforce": True}]
    monkeypatch.setattr(get_settings(), "sso_connections", json.dumps(conf))
    r = await client.post("/api/auth/email/signup", json={"email": "a@acme.com", "password": PW})
    assert r.status_code == 409 and "single sign-on" in r.json()["detail"]


def test_status_reports_the_new_sign_in_options(monkeypatch):
    import asyncio

    from app.auth import mailer, routes
    from app.config import get_settings

    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(get_settings(), "sso_connections", json.dumps([{"id": "acme", "issuer": "https://i.example.com", "client_id": "x", "domains": ["acme.com"]}]))
    out = asyncio.run(routes.oauth_status())
    assert out["email"] is True and out["sso"] is True
