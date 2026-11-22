"""Teams (docs/TEAMS.md): invitations for one address, roles, a shared skill becomes your own clean copy, and the
team's activity log. Against Postgres, with three signed-in people."""

from __future__ import annotations

import os
import re
import uuid

import pytest
from sqlalchemy import text

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")


@pytest.fixture
async def env(monkeypatch):
    import httpx
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.cache as cache
    from app.auth import mailer
    from app.db import get_db
    from app.deps import current_user
    from app.models import AgentSkill, Base, User
    from app.teams import routes

    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMPTZ"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tag = uuid.uuid4().hex[:8]
    async with factory() as ses:
        people = {n: User(email=f"{n}-{tag}@example.com", display_name=n.title(), primary_provider="email") for n in ("ana", "ben", "cy")}
        ses.add_all(people.values())
        await ses.flush()
        skill = AgentSkill(user_id=people["ana"].id, name="Weekly sales report", kind="taught", instruction="1. Open the store",
                           steps=[{"kind": "type", "text": "Sup3r$ecret!", "window": "Sign in"}, {"kind": "click", "x": 1}],
                           frames=[{"t": 1, "jpeg": "AAAA"}], transcript=[{"t": 0, "text": "my login is ..."}], status="ready")  # fmt: skip
        ses.add(skill)
        await ses.commit()

    def no_redis():
        raise RuntimeError("no redis")

    sent: list[dict] = []

    async def send(to, subject, body, page):
        sent.append({"to": to, "subject": subject, "text": body})
        return True

    monkeypatch.setattr(cache, "get_redis", no_redis)
    monkeypatch.setattr(mailer, "send", send)
    monkeypatch.setattr(mailer, "configured", lambda: True)

    async def db():
        async with factory() as ses:
            yield ses

    clients = {}
    for name, person in people.items():
        app = FastAPI()
        app.include_router(routes.router, prefix="/api")
        app.dependency_overrides[get_db] = db
        app.dependency_overrides[current_user] = (lambda p: (lambda: p))(person)
        clients[name] = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    yield {"c": clients, "people": people, "skill": skill, "sent": sent, "factory": factory}
    for c in clients.values():
        await c.aclose()
    async with factory() as ses:
        for p in people.values():
            await ses.execute(text("DELETE FROM teams WHERE owner_id = :i"), {"i": p.id})
            await ses.execute(text("DELETE FROM users WHERE id = :i"), {"i": p.id})
        await ses.commit()
    await engine.dispose()


def _token(sent) -> str:
    return re.search(r"/teams/join\?token=(\S+)", sent[-1]["text"]).group(1)


async def test_invite_join_roles_share_and_activity(env):
    ana, ben, cy = env["c"]["ana"], env["c"]["ben"], env["c"]["cy"]
    people = env["people"]
    r = await ana.post("/api/teams", json={"name": "  Sales   ops "})
    team = r.json()["id"]
    assert r.json()["name"] == "Sales ops" and r.json()["role"] == "owner"
    assert (await ben.get(f"/api/teams/{team}")).status_code == 404  # not a member: the team doesn't exist for him

    r = await ana.post(f"/api/teams/{team}/invites", json={"email": people["ben"].email.upper(), "role": "member"})
    assert r.status_code == 200 and r.json()["sent"] and env["sent"][-1]["to"] == people["ben"].email
    token = _token(env["sent"])
    r = await cy.post("/api/teams/join", json={"token": token})
    assert r.status_code == 403 and people["ben"].email in r.json()["detail"]  # an invite works only for its address
    assert (await ben.post("/api/teams/join", json={"token": token})).json()["name"] == "Sales ops"
    assert (await ben.post("/api/teams/join", json={"token": token})).status_code == 400  # used once
    assert (await ben.post(f"/api/teams/{team}/invites", json={"email": people["cy"].email})).status_code == 403  # members can't invite

    await ana.post(f"/api/teams/{team}/invites", json={"email": people["cy"].email, "role": "viewer"})
    await cy.post("/api/teams/join", json={"token": _token(env["sent"])})
    detail = (await ana.get(f"/api/teams/{team}")).json()
    assert {m["email"]: m["role"] for m in detail["members"]} == {people["ana"].email: "owner", people["ben"].email: "member",
                                                                   people["cy"].email: "viewer"}  # fmt: skip

    # Sharing: a viewer can't share; the shared copy has no screenshots, narration or typed secret.
    sid = str(env["skill"].id)
    assert (await ben.post(f"/api/teams/{team}/skills", json={"skill_id": sid})).status_code == 404  # not his skill
    assert (await ana.post(f"/api/teams/{team}/skills", json={"skill_id": sid})).status_code == 200
    shared = (await cy.get(f"/api/teams/{team}")).json()["skills"]
    assert shared[0]["name"] == "Weekly sales report" and shared[0]["shared_by"] == "Ana"
    r = await cy.post(f"/api/teams/{team}/skills/{shared[0]['id']}/add", json={})
    assert r.status_code == 200
    from app.models import AgentSkill

    async with env["factory"]() as ses:
        copy = await ses.get(AgentSkill, uuid.UUID(r.json()["skill_id"]))
        assert copy.user_id == people["cy"].id and not copy.frames and not copy.transcript
        assert "Sup3r$ecret!" not in str(copy.steps) and copy.source["team_name"] == "Sales ops"
    assert (await cy.post(f"/api/teams/{team}/skills", json={"skill_id": sid})).status_code == 403  # viewer can't share

    # Roles: only the owner makes admins; the owner can't be demoted or leave.
    ben_id, ana_id = str(people["ben"].id), str(people["ana"].id)
    assert (await ana.patch(f"/api/teams/{team}/members/{ben_id}", json={"role": "admin"})).status_code == 200
    assert (await ben.patch(f"/api/teams/{team}/members/{ana_id}", json={"role": "member"})).status_code == 400
    assert (await ana.delete(f"/api/teams/{team}/members/{ana_id}")).status_code == 400
    assert (await cy.delete(f"/api/teams/{team}/members/{str(people['cy'].id)}")).status_code == 200  # leaving

    log = (await ben.get(f"/api/teams/{team}/activity")).json()  # admins see the activity log
    actions = [a["action"] for a in log]
    assert {"created", "invited", "joined", "skill_shared", "skill_added", "role_changed", "left"} <= set(actions)
    csv = await ben.get(f"/api/teams/{team}/activity?fmt=csv")
    assert csv.status_code == 200 and "team_skill_shared" in csv.text
    assert (await cy.get(f"/api/teams/{team}/activity")).status_code == 404  # gone from the team

    # Ownership moves; then the new owner can delete.
    assert (await ana.post(f"/api/teams/{team}/owner", json={"user_id": ben_id})).status_code == 200
    assert (await ana.delete(f"/api/teams/{team}")).status_code == 403
    assert (await ben.delete(f"/api/teams/{team}")).status_code == 200
    assert (await ana.get("/api/teams")).json() == []


async def test_a_team_invitation_lets_its_address_sign_up_while_invite_only(env, monkeypatch):
    from app.auth import access
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "signup_mode", "invite")
    monkeypatch.setattr(get_settings(), "signup_allow", "")
    ana = env["c"]["ana"]
    team = (await ana.post("/api/teams", json={"name": "Ops"})).json()["id"]
    newcomer = f"newcomer-{uuid.uuid4().hex[:6]}@example.com"
    async with env["factory"]() as ses:
        assert not await access.may_sign_up_db(ses, newcomer)
    await ana.post(f"/api/teams/{team}/invites", json={"email": newcomer})
    async with env["factory"]() as ses:
        assert await access.may_sign_up_db(ses, newcomer.upper())
        assert not await access.may_sign_up_db(ses, "someone-else@example.com")
