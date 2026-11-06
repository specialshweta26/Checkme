"""Desktop sign-in (docs/LLD.md "Desktop sign-in"): sign-in started in the desktop app finishes in the system browser,
and the app collects a session of its own with its one-time code."""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.auth import routes
from app.security import OAUTH_COOKIE, SESSION_COOKIE, load_state, read_session_jwt
from tests.test_stop import FakeRedis

CODE = "A" * 20 + "b-_" + "c" * 20


class Provider:
    client_id = "cid"
    scopes = ["openid"]

    def authorization_url(self, state, challenge):
        return f"https://accounts.example.test/auth?state={state}"

    async def exchange_code(self, code, verifier):
        return {"access_token": "at", "expires_in": 3600, "scope": "openid"}

    async def userinfo(self, token):
        return {"subject": f"sub-{uuid.uuid4()}", "email": "jitu@example.test", "name": "Jitu"}


@pytest.fixture
def redis(monkeypatch):
    r = FakeRedis()

    class Pipe:
        def __init__(self):
            self.ops = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, k):
            self.ops.append(("get", k))
            return self

        def delete(self, k):
            self.ops.append(("delete", k))
            return self

        async def execute(self):
            out = []
            for op, k in self.ops:
                if op == "get":
                    out.append(r.kv.get(k))
                else:
                    out.append(1 if r.kv.pop(k, None) is not None else 0)
            return out

    r.pipeline = lambda transaction=True: Pipe()
    monkeypatch.setattr(routes, "get_redis", lambda: r)
    return r


def _app():
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    return app


def test_login_from_the_desktop_app_carries_its_code(monkeypatch):
    monkeypatch.setattr(routes, "get_provider", lambda name: Provider())
    client = TestClient(_app(), follow_redirects=False)
    resp = client.get(f"/api/auth/google/login?desktop={CODE}")
    assert resp.status_code in (302, 307) and resp.headers["location"].startswith("https://accounts.example.test/")
    state = load_state(resp.cookies.get(OAUTH_COOKIE))
    assert state["desktop"] == CODE and state["mode"] == "signin"
    for bad in ("short", "has spaces " * 4, "x" * 200):
        assert "invalid_state" in client.get(f"/api/auth/google/login?desktop={bad}").headers["location"]
    plain = load_state(client.get("/api/auth/google/login").cookies.get(OAUTH_COOKIE))
    assert plain["desktop"] is None


def test_claim_is_single_use_and_checks_the_code(redis):
    client = TestClient(_app())
    assert client.post("/api/auth/desktop/claim", json={"code": CODE}).json() == {"ok": False, "pending": True}
    assert client.post("/api/auth/desktop/claim", json={"code": "nope"}).status_code == 400
    redis.kv[f"desktop_signin:{CODE}"] = b"jwt-token"
    first = client.post("/api/auth/desktop/claim", json={"code": CODE})
    assert first.json() == {"ok": True} and first.cookies.get(SESSION_COOKIE) == "jwt-token"
    assert client.post("/api/auth/desktop/claim", json={"code": CODE}).json()["ok"] is False


URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")


@needs_db
async def test_browser_sign_in_leaves_a_separate_desktop_session_for_the_app(monkeypatch, redis):
    import httpx
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import get_db
    from app.models import AuthSession, Base

    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def db_dep():
        async with factory() as db:
            yield db

    async def noop(*a, **k):
        return None

    async def connector(db, user, account):
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(routes, "get_provider", lambda name: Provider())
    monkeypatch.setattr(routes, "ensure_builtin_connector", connector)
    monkeypatch.setattr(routes, "invalidate", noop)
    monkeypatch.setattr(routes, "seed_access_token", noop)
    monkeypatch.setattr(routes, "sync_in_background", noop)
    monkeypatch.setattr(routes, "seed_letta", noop)
    import app.connectors.platform as platform

    monkeypatch.setattr(platform, "ensure_platform_connectors", noop)
    app = _app()
    app.dependency_overrides[get_db] = db_dep
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://app.test") as browser:
        start = await browser.get(f"/api/auth/google/login?desktop={CODE}")
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        done = await browser.get(f"/api/auth/google/callback?code=c&state={state}")
        assert done.headers["location"].endswith("/desktop-signed-in")
        browser_jwt = read_session_jwt(done.cookies.get(SESSION_COOKIE))
        assert browser_jwt  # the browser is signed in too
    async with httpx.AsyncClient(transport=transport, base_url="http://app.test") as app_window:
        claimed = await app_window.post("/api/auth/desktop/claim", json={"code": CODE})
        desk_jwt = read_session_jwt(claimed.cookies.get(SESSION_COOKIE))
    assert claimed.json() == {"ok": True} and desk_jwt["sub"] == browser_jwt["sub"] and desk_jwt["sid"] != browser_jwt["sid"]
    async with factory() as db:
        row = await db.get(AuthSession, uuid.UUID(desk_jwt["sid"]))
        assert row is not None and row.user_agent == "Lumio desktop app"
        await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": uuid.UUID(desk_jwt["sub"])})
        await db.commit()
    await engine.dispose()
