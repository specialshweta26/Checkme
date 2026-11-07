"""Memory belongs to the user, against a real Postgres: shared by all of a user's agents, never visible to another
user, and safe when many turns write at the same time.

Skipped unless CHECKME_TEST_DATABASE_URL is set (CI sets it; see .github/workflows/api-tests.yml), e.g.
  CHECKME_TEST_DATABASE_URL=postgresql+asyncpg://assistant:assistant@localhost:5432/assistant_test uv run pytest -q tests/test_user_memory_db.py
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")


def _vec(body: str) -> list[float]:
    from app.models import DIMS

    v = [0.0] * DIMS
    for word in (body or "").lower().split():
        word = "".join(ch for ch in word if ch.isalnum())
        if word:
            v[int(hashlib.sha256(word.encode()).hexdigest(), 16) % DIMS] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


@pytest.fixture
async def factory(monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app import db as app_db
    from app.agent import memory
    from app.memory_engine import semantic
    from app.models import Base

    async def embed(body):
        return _vec(body)

    monkeypatch.setattr(memory, "embed_one", embed)
    monkeypatch.setattr("app.llm.embed_one", embed)
    monkeypatch.setattr(semantic, "_embed", embed)
    monkeypatch.setattr(memory, "_letta_down", lambda: True)
    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        await app_db._migrate_memory(conn)
        await app_db._migrate_user_memory(conn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _user_with_agents(factory, *names):
    from app.agents.service import ensure_default_agent
    from app.models import User, WorkspaceAgent

    async with factory() as db:
        user = User(email=f"{uuid.uuid4().hex[:8]}@users.test", display_name="Test", primary_provider="google")
        db.add(user)
        await db.flush()
        chief = await ensure_default_agent(db, user)
        agents = [chief]
        for name in names:
            agent = WorkspaceAgent(user_id=user.id, name=name, kind="custom")
            db.add(agent)
            agents.append(agent)
        await db.commit()
        return user, agents


# --- shared within a user ---------------------------------------------------------------------------------------


async def test_facts_and_notes_are_shared_by_all_of_a_users_agents(factory):
    from app.agent import memory

    user, (chief, home, dev) = await _user_with_agents(factory, "Home", "Dev")
    async with factory() as db:
        await memory.remember(db, user.id, "archival", "Lives in Pune", home.id)
        await memory.learn_for_agent(db, user, dev, "preference", "Prefers markdown tables")
        await memory.upsert_core(db, user.id, "voice", "Warm and brief", home.id)
        denied = await memory.learn_for_agent(db, user, dev, "secret", "Remember my password: hunter2")
        assert denied["saved"] is False
        for agent in (chief, home, dev):
            seen = await memory.load_memory(db, user, "where do I live and how should you answer", agent)
            assert "Lives in Pune" in seen and "markdown tables" in seen and "Warm and brief" in seen, agent.name
            assert "hunter2" not in seen
        listed = await memory.list_agent_memory(db, user, dev.id)
        assert any("Pune" in f["text"] for f in listed["shared_facts"])


async def test_an_agents_own_notes_stay_with_that_agent(factory):
    from app.agent import memory

    user, (_chief, home, dev) = await _user_with_agents(factory, "Home", "Dev")
    async with factory() as db:
        await memory.remember(db, user.id, "archival", "Dev runs pytest with -q before every push", dev.id, scope=memory.SCOPE_AGENT)
        await memory.upsert_core(db, user.id, "coding", "Default repo: acme/api", dev.id)
        dev_mem = await memory.load_memory(db, user, "pytest push repo", dev)
        home_mem = await memory.load_memory(db, user, "pytest push repo", home)
        assert "pytest" in dev_mem and "acme/api" in dev_mem
        assert "pytest" not in home_mem and "acme/api" not in home_mem


async def test_deleting_an_agent_keeps_the_users_memory(factory):
    from app.agent import memory
    from app.models import MemoryPassage, WorkspaceAgent

    user, (chief, home) = await _user_with_agents(factory, "Home")
    async with factory() as db:
        row = await memory.remember(db, user.id, "archival", "Daughter goes to Greenwood High", home.id)
        assert row.agent_id == chief.id  # kept on the user's default agent
        await db.delete(await db.get(WorkspaceAgent, home.id))
        await db.commit()
        kept = (await db.execute(select(MemoryPassage).where(MemoryPassage.id == row.id))).scalar_one_or_none()
        assert kept is not None and kept.status == "active"


# --- never across users -------------------------------------------------------------------------------------------


async def test_nothing_crosses_between_users(factory):
    from app.agent import memory
    from app.agent.deep import RunContext
    from app.memory_engine import entities, semantic
    from app.memory_engine.episodes import save_episode

    a, (a_chief, a_home) = await _user_with_agents(factory, "Home")
    b, (b_chief, b_home) = await _user_with_agents(factory, "Home")
    async with factory() as db:
        priya_a = await entities.resolve(db, a.id, name="Priya Nair", email="priya@acme.com")
        await memory.remember(db, a.id, "archival", "Priya is Head of Ops at Acme", a_home.id,
                              subject_entity_id=priya_a.id, predicate="role")
        await memory.remember(db, a.id, "archival", "Salary is 42 lakh", a_home.id)
        await memory.learn_for_agent(db, a, a_home, "preference", "Likes concise bullet points")
        await save_episode(db, a.id, a_home.id, {"observation": "A discussed the Acme merger", "outcome": "completed"})
        await semantic.store_document(db, a.id, body="Acme merger term sheet v3", title="term sheet", source="document", agent_id=a_home.id)
        await db.commit()
        a_fact_id = (await memory._ranked_facts(db, a.id, a_chief.id, "salary", limit=3))[0].id

        for agent in (b_chief, b_home):
            seen = await memory.load_memory(db, b, "Priya Acme salary merger bullet points term sheet", agent)
            for leaked in ("Priya", "Salary", "concise bullet", "merger", "term sheet"):
                assert leaked not in seen, (agent.name, leaked)
            assert await memory._ranked_facts(db, b.id, agent.id, "salary Priya", limit=10) == []
            assert await semantic.search_documents(db, b.id, agent.id, "Acme merger term sheet") == []
        ctx_b = RunContext(db=db, user=b, connectors={}, agent=b_home)
        recalled = await memory.recall_memory(ctx_b, query="salary")
        assert recalled["passages"] == []
        # B cannot forget, replace or retire A's fact by its id.
        assert (await memory.forget_memory(ctx_b, memory_id=str(a_fact_id))).get("error")
        assert await memory.retire_fact(db, b, b_home, a_fact_id) is False
        await memory.remember(db, b.id, "archival", "Salary is 10 lakh", b_home.id, supersedes_id=a_fact_id)
        still = await memory._ranked_facts(db, a.id, a_chief.id, "salary", limit=3)
        assert any(f.id == a_fact_id for f in still)
        # The same person in B's world is B's own entity; A's entity ids mean nothing to B.
        priya_b = await entities.resolve(db, b.id, name="Priya", email="priya@acme.com")
        assert priya_b.id != priya_a.id and priya_b.user_id == b.id
        assert await entities.names_for(db, b.id, {priya_a.id}) == {}


# --- many writers at once ------------------------------------------------------------------------------------------


async def test_the_same_fact_saved_by_concurrent_turns_is_stored_once(factory):
    from app.agent import memory
    from app.models import MemoryPassage

    user, (_chief, home, dev) = await _user_with_agents(factory, "Home", "Dev")

    async def save(agent):
        async with factory() as db:
            return await memory.remember(db, user.id, "archival", "Works as a product manager at Acme", agent.id)

    await asyncio.gather(*(save(agent) for agent in (home, dev, home, dev)))
    async with factory() as db:
        rows = (await db.execute(select(MemoryPassage).where(
            MemoryPassage.user_id == user.id, MemoryPassage.status == "active", MemoryPassage.text == "Works as a product manager at Acme"
        ))).scalars().all()
        assert len(rows) == 1
        assert rows[0].reinforced >= 2  # the other saves counted as confirmations


async def test_an_entity_is_created_once_under_concurrency(factory):
    from app.memory_engine import entities
    from app.models import MemoryEntity

    user, _agents = await _user_with_agents(factory)

    async def resolve():
        async with factory() as db:
            e = await entities.resolve(db, user.id, name="Rahul Mehta", email="rahul.mehta@zeta.co.in")
            await db.commit()
            return e.id

    ids = await asyncio.gather(*(resolve() for _ in range(6)))
    assert len(set(ids)) == 1
    async with factory() as db:
        people = (await db.execute(select(MemoryEntity).where(MemoryEntity.user_id == user.id, MemoryEntity.kind == "person"))).scalars().all()
        orgs = (await db.execute(select(MemoryEntity).where(MemoryEntity.user_id == user.id, MemoryEntity.kind == "org"))).scalars().all()
        assert len(people) == 1 and len(orgs) == 1
        assert people[0].org_id == orgs[0].id and orgs[0].name == "Zeta"
        personal = await entities.resolve(db, user.id, name="Sam", email="sam@gmail.com")
        assert personal.org_id is None  # personal mail says nothing about an employer
        moved = await entities.resolve(db, user.id, name="Rahul Mehta", email="rahul@acme.com")
        assert moved.id == people[0].id and (await db.get(MemoryEntity, moved.org_id)).name == "Acme"  # new job, new org


# --- claims replace each other ---------------------------------------------------------------------------------------


async def test_a_new_claim_replaces_the_old_one(factory):
    from app.agent import memory
    from app.memory_engine import entities

    user, (chief, home) = await _user_with_agents(factory, "Home")
    async with factory() as db:
        old = await memory.remember(db, user.id, "archival", "Lives in Bangalore", home.id, predicate="lives_in")
        new = await memory.remember(db, user.id, "archival", "Lives in Pune", home.id, predicate="lives_in")
        await db.refresh(old)
        assert old.status == "superseded" and old.valid_to is not None and new.supersedes_id == old.id
        priya = await entities.resolve(db, user.id, name="Priya", email="priya@acme.com")
        await memory.remember(db, user.id, "archival", "Priya works at Acme", home.id, subject_entity_id=priya.id, predicate="works_at")
        await memory.remember(db, user.id, "archival", "Priya works at Zeta", home.id, subject_entity_id=priya.id, predicate="works_at")
        facts = await memory._ranked_facts(db, user.id, chief.id, "where does Priya work where do I live", limit=10)
        texts = {f.text for f in facts}
        assert "Priya works at Zeta" in texts and "Lives in Pune" in texts
        assert "Priya works at Acme" not in texts and "Lives in Bangalore" not in texts
        seen = await memory.load_memory(db, user, "where does Priya work", home)
        assert "[about Priya] Priya works at Zeta" in seen
        twice = await memory.remember(db, user.id, "archival", "lives in pune", home.id, trust="user_said")
        assert twice.id == new.id and twice.reinforced == 2 and twice.confidence > 0.9


# --- the memory worker -----------------------------------------------------------------------------------------------


async def test_memory_jobs_run_one_per_user_and_stale_ones_come_back(factory, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.agent import memory_worker
    from app.models import MemoryExtractJob

    monkeypatch.setattr(memory_worker, "SessionLocal", factory)
    a, (a_chief,) = await _user_with_agents(factory)
    b, (b_chief,) = await _user_with_agents(factory)
    async with factory() as db:
        await db.execute(text("UPDATE memory_extract_jobs SET status = 'done' WHERE status IN ('queued', 'running')"))
        for user, agent, n in ((a, a_chief, 3), (b, b_chief, 2)):
            for i in range(n):
                db.add(MemoryExtractJob(user_id=user.id, agent_id=agent.id, request_id=f"r-{uuid.uuid4().hex[:8]}", payload={}))
        await db.commit()
    first = await memory_worker.claim(limit=4)
    async with factory() as db:
        claimed = (await db.execute(select(MemoryExtractJob).where(MemoryExtractJob.id.in_(first)))).scalars().all()
        assert sorted(str(j.user_id) for j in claimed) == sorted([str(a.id), str(b.id)])  # one each
    assert await memory_worker.claim(limit=4) == []  # both users busy: nothing more until those finish
    async with factory() as db:
        old = datetime.now(timezone.utc) - timedelta(seconds=memory_worker.STALE_S + 60)
        await db.execute(text("UPDATE memory_extract_jobs SET updated_at = :t WHERE id = ANY(:ids)"), {"t": old, "ids": first})
        await db.commit()
    again = await memory_worker.claim(limit=4)
    assert len(again) == 2  # the stuck jobs were put back and each user got one again


# --- migrating existing memory ----------------------------------------------------------------------------------------


async def test_existing_per_agent_memory_becomes_the_users(factory):
    from app import db as app_db
    from app.models import MemoryBlock, MemoryPassage

    user, (chief, home) = await _user_with_agents(factory, "Home")
    async with factory() as db:
        db.add(MemoryPassage(user_id=user.id, agent_id=home.id, kind="archival", text="Old: lives in Delhi", scope="agent", status="active"))
        db.add(MemoryPassage(user_id=user.id, agent_id=home.id, kind="archival", text="Old: lives in Delhi", scope="user_shared", status="active"))
        db.add(MemoryBlock(user_id=user.id, agent_id=home.id, label="prefs", value="Short answers"))
        db.add(MemoryBlock(user_id=user.id, agent_id=chief.id, label="prefs", value="Use tables"))
        db.add(MemoryBlock(user_id=user.id, agent_id=home.id, label="coding", value="repo acme/api"))
        await db.commit()
    async with factory() as db:
        await db.execute(text("DROP INDEX IF EXISTS uq_memory_passages_user_fact"))
        await db.execute(text("DELETE FROM app_migrations WHERE name = 'user_level_memory_v1'"))
        await db.commit()
    engine = factory.kw["bind"]
    async with engine.begin() as conn:
        await app_db._migrate_user_memory(conn)
    async with factory() as db:
        facts = (await db.execute(select(MemoryPassage).where(MemoryPassage.user_id == user.id, MemoryPassage.text == "Old: lives in Delhi"))).scalars().all()
        active = [f for f in facts if f.status == "active"]
        assert len(active) == 1 and active[0].scope == "user" and active[0].agent_id == chief.id
        blocks = {(b.agent_id, b.label): b.value for b in (await db.execute(select(MemoryBlock).where(MemoryBlock.user_id == user.id))).scalars()}
        assert blocks[(chief.id, "prefs")].splitlines() == ["Use tables", "Short answers"]
        assert (home.id, "prefs") not in blocks
        assert blocks[(home.id, "coding")] == "repo acme/api"  # per-agent notes stay put


# --- forgetting ------------------------------------------------------------------------------------------------------


async def test_forget_removes_a_person_everywhere_for_that_user_only(factory):
    from app.agent import memory
    from app.agent.deep import RunContext
    from app.memory_engine import entities
    from app.memory_engine.episodes import save_episode
    from app.models import MemoryEntity

    a, (a_chief, a_home) = await _user_with_agents(factory, "Home")
    b, (b_chief,) = await _user_with_agents(factory)
    async with factory() as db:
        for user, agent in ((a, a_home), (b, b_chief)):
            priya = await entities.resolve(db, user.id, name="Priya Nair", email="priya@example.com")
            await memory.remember(db, user.id, "archival", "COO at Zeta Corp", agent.id, subject_entity_id=priya.id, predicate="role")
            await memory.learn_for_agent(db, user, agent, "work", "My manager is Priya Nair")
            await memory.learn_for_agent(db, user, agent, "preference", "Prefers short bullet-point answers")
            await save_episode(db, user.id, agent.id, {"observation": "Talked about Priya Nair's promotion", "outcome": "completed"})
        await db.commit()

        ctx = RunContext(db=db, user=a, connectors={}, agent=a_home)
        out = await memory.forget_memory(ctx, text="Priya Nair")
        assert out["ok"] and any(r.startswith("entity:") for r in out["removed"])
        out = await memory.forget_memory(ctx, text="bullet-point")
        assert out["ok"]

        for agent in (a_chief, a_home):
            seen = await memory.load_memory(db, a, "Priya Nair role manager promotion bullet answers", agent)
            assert "Priya" not in seen and "Zeta" not in seen and "bullet" not in seen, seen
        people = (await db.execute(select(MemoryEntity).where(
            MemoryEntity.user_id == a.id, MemoryEntity.status == "active", MemoryEntity.kind == "person"))).scalars().all()
        assert people == []  # the person is gone; the organisation from her email domain stays
        # Another user's identical memory is untouched.
        b_seen = await memory.load_memory(db, b, "Priya Nair role manager bullet answers", b_chief)
        assert "Priya" in b_seen and "bullet" in b_seen
        # A new mention after forgetting starts a fresh record.
        again = await entities.resolve(db, a.id, name="Priya Nair", email="priya@example.com")
        assert again is not None and again.status == "active"
