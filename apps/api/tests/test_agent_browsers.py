"""A separate browser per agent (docs/AGENT_BROWSERS.md).

The daemon side (real Chrome, one profile per agent) is covered by test_agent_browsers_daemon.py when Playwright is
installed; these tests cover the API side.
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.agent_browsers import service as browsers
from app.connectors import daytona_box

# ---------------------------------------------------------------- no database


def test_space_names_come_from_the_agent_and_its_reset_count():
    aid = uuid.uuid4()
    assert browsers.space_for(SimpleNamespace(id=aid, browser={})) == ""  # shared by default
    assert browsers.space_for(SimpleNamespace(id=aid, browser={"mode": "shared", "epoch": 3})) == ""
    assert browsers.space_for(SimpleNamespace(id=aid, browser={"mode": "own"})) == f"a{aid.hex}-0"
    assert browsers.space_for(SimpleNamespace(id=aid, browser={"mode": "own", "epoch": 2})) == f"a{aid.hex}-2"
    assert browsers.space_for(None) == "" and browsers.space_for(SimpleNamespace(id=aid, browser="junk")) == ""
    assert browsers.settings(SimpleNamespace(browser={"mode": "nope", "epoch": "x"})) == {"mode": "shared", "epoch": 0}

    from app.connectors.computer_daemon import _SPACE_RE

    # The daemon turns a space into a folder name: what the API sends must pass its check.
    assert _SPACE_RE.match(browsers.space_for(SimpleNamespace(id=aid, browser={"mode": "own", "epoch": 99})))


def test_a_run_sets_its_browser_and_a_shared_teammate_goes_back_to_shared():
    own = SimpleNamespace(id=uuid.uuid4(), browser={"mode": "own"})
    shared = SimpleNamespace(id=uuid.uuid4(), browser={})
    assert browsers.current() == ("", "")
    outer = browsers.use(own)
    try:
        assert browsers.current() == (browsers.space_for(own), str(own.id))
        inner = browsers.use(shared)
        assert browsers.current() == ("", str(shared.id))
        browsers.BROWSER_SPACE.reset(inner)
        assert browsers.current()[0] == browsers.space_for(own)
    finally:
        browsers.BROWSER_SPACE.reset(outer)
    assert browsers.current() == ("", "")


def test_browser_commands_carry_the_agents_browser():
    own = SimpleNamespace(id=uuid.uuid4(), browser={"mode": "own"})
    space = browsers.space_for(own)
    assert daytona_box.with_space({"command": "navigate", "url": "https://x"}) == {"command": "navigate", "url": "https://x"}
    token = browsers.use(own)
    try:
        for command in ("navigate", "snapshot", "act", "tabs", "new_tab", "export_state", "fetch_download", "observe"):
            assert daytona_box.with_space({"command": command})["space"] == space
        for command in ("desktop_act", "status", "wake", "rec_start", "serp", "remote_fetch"):
            assert "space" not in daytona_box.with_space({"command": command})  # about the whole computer
        assert daytona_box.with_space({"command": "import_state", "space": "other"})["space"] == "other"
    finally:
        browsers.BROWSER_SPACE.reset(token)


def test_each_agent_browser_keeps_its_own_tab_map():
    from app.agent import browser_tabs

    uid = uuid.uuid4()
    own = SimpleNamespace(id=uuid.uuid4(), browser={"mode": "own"})
    assert browser_tabs._key(uid, "cloud") == f"browser:tabs:{uid}:cloud"
    token = browsers.use(own)
    try:
        assert browser_tabs._key(uid, "cloud") == f"browser:tabs:{uid}:cloud:{browsers.space_for(own)}"
        assert browser_tabs._key(uid, "desktop") == f"browser:tabs:{uid}:desktop:{browsers.space_for(own)}"
    finally:
        browsers.BROWSER_SPACE.reset(token)


async def test_a_fresh_agent_browser_gets_its_saved_cookies_back(monkeypatch):
    from app.connectors import browser_profile

    own = SimpleNamespace(id=uuid.uuid4(), browser={"mode": "own"})
    space = browsers.space_for(own)
    sent: list[dict] = []

    async def ensure(proc, fs=None):
        return None

    async def locked(proc, payload, timeout, fs=None):
        sent.append(payload)
        return {"ok": True, "space_fresh": True} if payload["command"] == "navigate" else {"ok": True, "imported": 1}

    async def load_agent(agent_id, sp):
        return [{"name": "sid", "value": "1", "domain": "example.com"}] if (agent_id, sp) == (str(own.id), space) else []

    monkeypatch.setattr(daytona_box, "_ensure_daemon", ensure)
    monkeypatch.setattr(daytona_box, "_browser_rpc_locked", locked)
    monkeypatch.setattr(browser_profile, "load_agent", load_agent)
    token = browsers.use(own)
    try:
        out = await daytona_box._browser_rpc(object(), {"command": "navigate", "url": "https://example.com"})
    finally:
        browsers.BROWSER_SPACE.reset(token)
    assert "space_fresh" not in out
    assert [p["command"] for p in sent] == ["navigate", "import_state"]
    assert sent[0]["space"] == space and sent[1]["space"] == space and sent[1]["cookies"][0]["name"] == "sid"


# ---------------------------------------------------------------- against a real database (CHECKME_TEST_DATABASE_URL)

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")


@pytest.fixture
async def db(monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.db
    from app.models import Base, User, WorkspaceAgent

    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        for col in ("accepts_from", "limits", "browser"):
            await conn.execute(text(f"ALTER TABLE workspace_agents ADD COLUMN IF NOT EXISTS {col} JSONB DEFAULT '{{}}'::jsonb"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(app.db, "SessionLocal", factory)
    uid, other, raaz = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(User(id=uid, email=f"{uid}@test.local", primary_provider="google"))
        s.add(User(id=other, email=f"{other}@test.local", primary_provider="google"))
        await s.flush()
        s.add(WorkspaceAgent(id=raaz, user_id=uid, name="Raaz"))
        await s.commit()
    yield factory, uid, other, raaz
    async with factory() as s:
        await s.execute(text("DELETE FROM users WHERE id IN (:u, :o)"), {"u": uid, "o": other})
        await s.commit()
    await engine.dispose()


@needs_db
async def test_own_browser_setting_saved_cookies_and_sign_out(db):
    from app.agent_browsers import routes
    from app.connectors import browser_profile
    from app.models import AgentBrowserProfile, User, WorkspaceAgent

    factory, uid, other, raaz = db
    cookies = [{"name": "sid", "value": "secret", "domain": "example.com", "path": "/"}]
    async with factory() as s:
        user = await s.get(User, uid)
        view = await routes.get_browser(raaz, user=user, db=s)
        assert view["mode"] == "shared" and view["saved_cookies"] == 0
        with pytest.raises(HTTPException) as refused:
            await routes.sign_out_everywhere(raaz, user=user, db=s)  # nothing of its own to sign out of
        assert refused.value.status_code == 400

        view = await routes.set_mode(raaz, routes.ModeIn(mode="own"), user=user, db=s)
        assert view["mode"] == "own"
        agent = await s.get(WorkspaceAgent, raaz)
        space = browsers.space_for(agent)
        assert space.endswith("-0")

    # Cookies are saved encrypted per agent and only come back for the same profile.
    assert await browser_profile.save_agent(str(raaz), str(uid), space, cookies)
    assert not await browser_profile.save_agent(str(raaz), str(other), space, cookies)  # not this user's agent
    assert (await browser_profile.load_agent(str(raaz), space))[0]["value"] == "secret"
    assert await browser_profile.load_agent(str(raaz), "a-other-0") == []
    async with factory() as s:
        row = await s.get(AgentBrowserProfile, raaz)
        assert row.cookie_count == 1 and "secret" not in row.state_enc
        view = await routes.get_browser(raaz, user=await s.get(User, uid), db=s)
        assert view["saved_cookies"] == 1 and view["saved_at"]

    # Another user can't see or change it.
    async with factory() as s:
        with pytest.raises(HTTPException) as missing:
            await routes.set_mode(raaz, routes.ModeIn(mode="shared"), user=await s.get(User, other), db=s)
        assert missing.value.status_code == 404

    # Sign out of everything: a new, empty profile and no saved cookies.
    async with factory() as s:
        view = await routes.sign_out_everywhere(raaz, user=await s.get(User, uid), db=s)
        assert view["mode"] == "own" and view["saved_cookies"] == 0
        agent = await s.get(WorkspaceAgent, raaz)
        assert browsers.space_for(agent).endswith("-1") and browsers.space_for(agent) != space
        assert await s.get(AgentBrowserProfile, raaz) is None
    assert await browser_profile.load_agent(str(raaz), space) == []

    # Back to shared: no space, and the agent row says so.
    async with factory() as s:
        view = await routes.set_mode(raaz, routes.ModeIn(mode="shared"), user=await s.get(User, uid), db=s)
        agent = await s.get(WorkspaceAgent, raaz)
        assert view["mode"] == "shared" and browsers.space_for(agent) == "" and agent.browser["epoch"] == 1

    from app.agents.service import serialize_agent

    assert serialize_agent(agent)["browser"] == {"mode": "shared"}


# ---------------------------------------------------------------- the desktop app


async def test_desktop_browser_commands_carry_the_agents_browser_or_are_refused(monkeypatch):
    from app.desktop import agent as desktop

    own = SimpleNamespace(id=uuid.uuid4(), browser={"mode": "own"})
    sent: list[tuple[str, dict]] = []
    features = {"value": ""}

    async def online(user_id):
        return True

    async def has_feature(user_id, name):
        return name in features["value"].split(",")

    async def enqueue(user_id, tool, args):
        sent.append((tool, args))
        return "job-1"

    async def wait_result(job_id, timeout_s):
        return {"ok": True}

    monkeypatch.setattr(desktop, "is_online", online)
    monkeypatch.setattr(desktop, "has_feature", has_feature)
    monkeypatch.setattr(desktop, "enqueue", enqueue)
    monkeypatch.setattr(desktop, "wait_result", wait_result)
    monkeypatch.setattr(desktop, "_ingest_screen", lambda user_id, out: out)

    # Shared agent: unchanged, whatever the app supports.
    assert (await desktop.call_local("u", "browser_navigate", {"url": "https://x"}))["ok"]
    assert sent[-1] == ("browser_navigate", {"url": "https://x"})

    token = browsers.use(own)
    try:
        # An older desktop app: refused, never run in the shared browser.
        out = await desktop.call_local("u", "browser_navigate", {"url": "https://x"})
        assert out["ok"] is False and out["needs_desktop_update"] and len(sent) == 1
        # Other tools on the computer still run.
        assert (await desktop.call_local("u", "list_dir", {"path": "~"}))["ok"] and sent[-1][1] == {"path": "~"}
        # An app that keeps agents' browsers apart gets the agent's browser with each command.
        features["value"] = "browser-spaces"
        for tool in ("browser_navigate", "browser_snapshot", "browser_tabs", "browse_act"):
            assert (await desktop.call_local("u", tool, {}))["ok"]
            assert sent[-1] == (tool, {"space": browsers.space_for(own)})
    finally:
        browsers.BROWSER_SPACE.reset(token)


async def test_the_desktop_app_says_what_it_supports(monkeypatch):
    from app.desktop import agent as desktop

    store: dict = {}

    class Redis:
        async def set(self, key, value, ex=None):
            store[key] = value

        async def get(self, key):
            return store.get(key)

    monkeypatch.setattr(desktop, "get_redis", lambda: Redis())
    await desktop.remember_features("u1", "browser-spaces, other")
    assert await desktop.has_feature("u1", "browser-spaces")
    await desktop.remember_features("u2", None)  # an older app sends nothing
    assert not await desktop.has_feature("u2", "browser-spaces")
    assert not await desktop.has_feature("u3", "browser-spaces")
