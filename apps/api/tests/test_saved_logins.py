"""Saved sign-ins (docs/BROWSING.md "Saved sign-ins"): stored encrypted, typed by code, never seen by the model."""

from __future__ import annotations

import json
import os
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.agent import fast_browser as fb
from app.browser_runs import logins, routes
from app.deps import current_user

USER = SimpleNamespace(id=uuid.uuid4())


def test_hosts_and_hints():
    assert logins.normalize_host("https://www.Portal.Example.com/login?x=1") == "portal.example.com"
    assert logins.normalize_host("portal.example.com") == "portal.example.com"
    assert logins.hint("gaurav@example.com") == "g•••@example.com" and logins.hint("admin") == "ad•••"
    assert logins._host_matches("example.com", "app.example.com")
    assert not logins._host_matches("app.example.com", "example.com")
    assert not logins._host_matches("example.com", "badexample.com")


async def test_browser_task_types_a_saved_login_the_planner_never_sees(monkeypatch):
    async def saved(user_id, host):
        assert host == "portal.example.com"
        return {logins.USERNAME: "ops@example.com", logins.PASSWORD: "s3cret-pass"}

    monkeypatch.setattr(logins, "values_for", saved)
    briefs, typed = [], []

    async def plan_step(model, goal, state, values, last, allow_final):
        briefs.append(fb.FAST_SYSTEM % (", ".join(f"{{{{{k}}}}}" for k in values), "") + fb.page_brief(state))
        return {"actions": [{"kind": "fill", "ref": "e1", "text": "{{login_username}}"}, {"kind": "fill", "ref": "e2", "text": "{{login_password}}"},
                            {"kind": "click", "ref": "e3"}], "done": True, "result": "Signed in as ops@example.com", "need_user": ""}  # fmt: skip

    async def act(c, actions):
        typed.extend(a.get("text") for a in actions)
        return {"ok": True, "url": "https://portal.example.com/home", "title": "Home", "text": "Welcome ops@example.com", "tree": "",
                "completed": 3, "trace": [{"i": 0, "target": {"name_attr": "user"}}, {"i": 1, "target": {"input_type": "password"}, "secret": True},
                                          {"i": 2, "target": {"role": "button", "name": "Sign in"}}]}  # fmt: skip

    async def nav(c, url):
        return {"ok": True, "url": url, "title": "Sign in", "text": "", "tree": "- e1 textbox User\n- e2 textbox Password [password]\n- e3 button Sign in"}

    class Store:
        saved = []

        async def candidates(self, *a, **k):
            return []

        async def save(self, user_id, **kw):
            self.saved.append(kw)
            return "t1"

    monkeypatch.setattr(fb, "plan_step", plan_step)
    monkeypatch.setattr(fb, "_act", act)
    monkeypatch.setattr(fb, "_navigate", nav)
    monkeypatch.setattr(fb, "STORE", Store())
    out = await fb.browser_task(SimpleNamespace(user=USER), "Sign in to the portal", "https://portal.example.com/login", {})
    assert out["done"] and typed[:2] == ["ops@example.com", "s3cret-pass"]
    assert "{{login_username}}" in briefs[0] and "{{login_password}}" in briefs[0]
    blob = json.dumps(out) + "".join(briefs) + json.dumps(Store.saved)
    assert "s3cret-pass" not in blob and "ops@example.com" not in blob
    # The saved run keeps only the names, so the next replay fills them again.
    assert [s.get("param") for s in Store.saved[0]["steps"][:2]] == ["login_username", "login_password"]


async def test_caller_values_win_over_a_saved_login(monkeypatch):
    async def saved(user_id, host):
        return {logins.USERNAME: "saved", logins.PASSWORD: "saved-pass"}

    monkeypatch.setattr(logins, "values_for", saved)
    vals = {logins.USERNAME: "mine"}
    assert await fb.add_saved_login(USER.id, "example.com", vals)
    assert vals == {logins.USERNAME: "mine", logins.PASSWORD: "saved-pass"}
    assert not await fb.add_saved_login(USER.id, "", {})


def test_prompt_never_asks_for_a_password_in_chat():
    hint = fb.prompt_hint()
    assert "{{login_password}}" in hint and "Never ask the user to type a password in chat" in hint


@pytest.fixture
def client(monkeypatch):
    calls = []

    async def list_logins(user_id):
        return [{"id": "l1", "host": "example.com", "label": "Example", "username_hint": "g•••@example.com"}]

    async def save_login(user_id, host, username, password, label=""):
        calls.append((host, username, bool(password), label))
        if not host:
            raise ValueError("give the site address, for example portal.example.com")
        return {"id": "l2", "host": host, "label": label or host, "username_hint": logins.hint(username)}

    async def delete_login(user_id, login_id):
        return login_id == "l1"

    monkeypatch.setattr(logins, "list_logins", list_logins)
    monkeypatch.setattr(logins, "save_login", save_login)
    monkeypatch.setattr(logins, "delete_login", delete_login)
    app = FastAPI()
    app.include_router(routes.login_router, prefix="/api")
    app.dependency_overrides[current_user] = lambda: USER
    return SimpleNamespace(http=TestClient(app), calls=calls)


def test_login_routes_never_return_a_password(client):
    assert client.http.get("/api/browser/logins").json()[0]["username_hint"] == "g•••@example.com"
    out = client.http.post("/api/browser/logins", json={"host": "portal.example.com", "username": "ops@example.com", "password": "pw"})
    assert out.status_code == 200 and "pw" not in out.text and "ops@example.com" not in out.text
    assert client.http.post("/api/browser/logins", json={"host": "", "username": "x", "password": "y"}).status_code == 400
    assert client.http.delete("/api/browser/logins/l1").json() == {"ok": True}
    assert client.http.delete("/api/browser/logins/zz").status_code == 404


# --- against a real database (set CHECKME_TEST_DATABASE_URL) ------------------------------------------------------
URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")


@pytest.fixture
async def db_user(monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.db as appdb
    from app.models import Base, User

    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(appdb, "SessionLocal", factory)
    uid = uuid.uuid4()
    async with factory() as db:
        db.add(User(id=uid, email=f"{uid}@test.local", primary_provider="google"))
        await db.commit()
    yield uid, factory
    async with factory() as db:
        await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": uid})
        await db.commit()
    await engine.dispose()


@needs_db
async def test_logins_are_encrypted_scoped_and_matched_by_site(db_user):
    uid, factory = db_user
    row = await logins.save_login(uid, "https://www.portal.example.com/login", "ops@example.com", "s3cret-pass", "Portal")
    assert row["host"] == "portal.example.com" and row["username_hint"] == "o•••@example.com"
    async with factory() as db:
        raw = (await db.execute(text("SELECT username_enc, password_enc FROM site_logins WHERE user_id = :u"), {"u": uid})).one()
    assert "s3cret-pass" not in raw[1] and "ops@example.com" not in raw[0]
    assert await logins.values_for(uid, "https://portal.example.com/x") == {"login_username": "ops@example.com", "login_password": "s3cret-pass"}
    assert await logins.values_for(uid, "https://sso.portal.example.com/") != {}
    assert await logins.values_for(uid, "https://example.com/") == {}
    assert await logins.values_for(uuid.uuid4(), "https://portal.example.com/") == {}
    # A new username with an empty password keeps the saved password.
    await logins.save_login(uid, "portal.example.com", "ops2@example.com", "")
    assert (await logins.values_for(uid, "portal.example.com"))["login_password"] == "s3cret-pass"
    with pytest.raises(ValueError):
        await logins.save_login(uid, "new.example.com", "x", "")
    listed = await logins.list_logins(uid)
    assert len(listed) == 1 and listed[0]["last_used_at"] and "s3cret" not in json.dumps(listed)
    assert not await logins.delete_login(uuid.uuid4(), listed[0]["id"])
    assert await logins.delete_login(uid, listed[0]["id"]) and await logins.list_logins(uid) == []
