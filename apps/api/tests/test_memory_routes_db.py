"""What I know (/api/memory), against a real Postgres: each user sees and changes only their own memory; edits
replace facts and keep history; the weekly change list can be undone; forget removes every copy.

Skipped unless CHECKME_TEST_DATABASE_URL is set (see tests/test_user_memory_db.py).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from tests.test_user_memory_db import _user_with_agents, factory  # noqa: F401 - the fixture is used by name

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")


def _client(factory, user):
    from app.db import get_db
    from app.deps import current_user
    from app.memory_engine.routes import router

    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def db_dep():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db] = db_dep
    app.dependency_overrides[current_user] = lambda: user
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app.test")


async def _seed(factory, user, chief, name="Priya Nair", email="priya@acme.com"):
    from app.agent import memory
    from app.memory_engine.entities import resolve

    async with factory() as db:
        p = await resolve(db, user.id, name=name, email=email)
        p.mail_out, p.salience, p.summary = 5, 2.0, f"{name}: Head of Ops at Acme."
        await db.commit()
        fact = await memory.remember(db, user.id, "archival", f"{name} is Head of Ops at Acme", chief.id,
                                     subject_entity_id=p.id, predicate="role")
        own = await memory.remember(db, user.id, "archival", "User lives in Bangalore", chief.id, predicate="lives_in")
        return p.id, fact.id, own.id


async def test_each_user_sees_only_their_own_memory(factory):
    a, (chief_a,) = await _user_with_agents(factory)
    b, (chief_b,) = await _user_with_agents(factory)
    pa, fa, _ = await _seed(factory, a, chief_a)
    pb, fb, _ = await _seed(factory, b, chief_b, "Rahul Mehta", "rahul@zeta.com")
    async with _client(factory, a) as http:
        people = (await http.get("/api/memory/people")).json()
        assert [p["name"] for p in people["people"]] == ["Priya Nair"]
        assert people["people"][0]["card"] == "Priya Nair: Head of Ops at Acme." and people["people"][0]["emails"] == ["priya@acme.com"]
        detail = (await http.get(f"/api/memory/people/{pa}")).json()
        assert detail["current"][0]["text"] == "Priya Nair is Head of Ops at Acme"
        assert {f["text"] for f in (await http.get("/api/memory/facts")).json()["facts"]} == {
            "Priya Nair is Head of Ops at Acme", "User lives in Bangalore"}
        # Another user's ids are not found, for reading and for changing.
        assert (await http.get(f"/api/memory/people/{pb}")).status_code == 404
        assert (await http.patch(f"/api/memory/facts/{fb}", json={"text": "x"})).status_code == 404
        assert (await http.delete(f"/api/memory/facts/{fb}")).status_code == 404
        assert (await http.delete(f"/api/memory/people/{pb}")).status_code == 404
        b_changes = [c["id"] for c in (await _client(factory, b).get("/api/memory/changes")).json()["changes"]]
        assert (await http.post(f"/api/memory/changes/{b_changes[0]}/undo")).status_code == 404


async def test_edit_keeps_history_and_undo_puts_it_back(factory):
    from app.models import MemoryPassage

    a, (chief,) = await _user_with_agents(factory)
    _, _, own = await _seed(factory, a, chief)
    async with _client(factory, a) as http:
        edited = (await http.patch(f"/api/memory/facts/{own}", json={"text": "User lives in Pune"})).json()
        assert edited["fact"]["trust"] == "user_said"
        texts = {f["text"] for f in (await http.get("/api/memory/facts")).json()["facts"]}
        assert "User lives in Pune" in texts and "User lives in Bangalore" not in texts
        digest = (await http.get("/api/memory/changes")).json()
        sup = next(c for c in digest["changes"] if c["op"] == "supersede")
        assert (sup["before"], sup["after"], sup["can_undo"]) == ("User lives in Bangalore", "User lives in Pune", True)
        assert digest["counts"]["add"] >= 3 and digest["counts"]["supersede"] == 1
        assert (await http.post(f"/api/memory/changes/{sup['id']}/undo")).json() == {"ok": True}
        texts = {f["text"] for f in (await http.get("/api/memory/facts")).json()["facts"]}
        assert "User lives in Bangalore" in texts and "User lives in Pune" not in texts
        assert (await http.post(f"/api/memory/changes/{sup['id']}/undo")).status_code == 409  # only once
    # A faded guess comes back on undo, confirmed so it doesn't fade again straight away.
    from app.memory_engine import consolidate

    async with factory() as db:
        guess = MemoryPassage(user_id=a.id, agent_id=chief.id, kind="archival", text="User may like jazz", scope="user",
                              trust="inferred", confidence=0.6, valid_from=datetime.now(timezone.utc) - timedelta(days=600))
        db.add(guess)
        await db.commit()
        assert await consolidate.fade(db, a.id) == 1
        await db.commit()
    async with _client(factory, a) as http:
        fade = next(c for c in (await http.get("/api/memory/changes")).json()["changes"] if c["op"] == "fade")
        await http.post(f"/api/memory/changes/{fade['id']}/undo")
        assert "User may like jazz" in {f["text"] for f in (await http.get("/api/memory/facts")).json()["facts"]}
    async with factory() as db:
        assert await consolidate.fade(db, a.id) == 0


async def test_this_is_me_and_forget_everywhere(factory):
    from app.models import MemoryChange, MemoryPassage

    a, (chief,) = await _user_with_agents(factory)
    pid, fact, _ = await _seed(factory, a, chief, "gaurav grv", "gaurav.grv@gmail.com")
    async with _client(factory, a) as http:
        assert (await http.post(f"/api/memory/people/{pid}/me", json={"is_me": True})).json()["is_me"] is True
        listed = (await http.get("/api/memory/people")).json()
        assert listed["people"] == [] and listed["me"][0]["emails"] == ["gaurav.grv@gmail.com"]
        await http.post(f"/api/memory/people/{pid}/me", json={"is_me": False})
        assert [p["name"] for p in (await http.get("/api/memory/people")).json()["people"]] == ["gaurav grv"]
        assert (await http.delete(f"/api/memory/people/{pid}")).json()["ok"] is True
        assert (await http.get("/api/memory/people")).json() == {"people": [], "me": []}
        assert (await http.get(f"/api/memory/people/{pid}")).status_code == 404
    async with factory() as db:
        assert (await db.get(MemoryPassage, fact)).status == "deleted"
        logged = (await db.execute(select(MemoryChange).where(MemoryChange.user_id == a.id))).scalars().all()
        assert not [c for c in logged if "gaurav" in (c.before + c.after).lower()]  # no copies left in the log
