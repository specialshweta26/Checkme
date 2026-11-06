"""Every device shows the same chat state (docs/LLD.md "Turn status"), against a real database.

The case from a real chat: an old turn stuck as "working" (its feed long expired) followed by newer finished
replies kept the web app showing "Chief is working", and pressing Stop brought it straight back."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")


@pytest.fixture
async def chat_db(monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.cache
    from app.models import Base, ChatSession, User
    from tests.test_stop import FakeRedis

    redis = FakeRedis()
    monkeypatch.setattr(app.cache, "get_redis", lambda: redis)
    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    uid = uuid.uuid4()
    async with factory() as db:
        db.add(User(id=uid, email=f"{uid}@test.local", primary_provider="google"))
        await db.flush()
        sess = ChatSession(user_id=uid, title="t", state={})
        db.add(sess)
        await db.commit()
    yield SimpleNamespace(factory=factory, user=SimpleNamespace(id=uid), sess_id=sess.id, redis=redis)
    async with factory() as db:
        await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": uid})
        await db.commit()
    await engine.dispose()


async def _add(db, env, role, content, meta=None, age_min=0):
    from app.models import ChatMessage

    m = ChatMessage(session_id=env.sess_id, user_id=env.user.id, role=role, content=content, meta=meta or {},
                    created_at=datetime.now(timezone.utc) - timedelta(minutes=age_min))  # fmt: skip
    db.add(m)
    await db.flush()
    return m


@needs_db
async def test_an_old_stuck_turn_is_settled_when_the_chat_loads_and_on_status_checks(chat_db):
    from app.chat import routes

    env = chat_db
    async with env.factory() as db:
        await _add(db, env, "user", "Hello", age_min=90)
        stuck = await _add(db, env, "assistant", "Chief is working on this…", {"in_progress": True, "progress_id": "turn_stuckold0001"}, age_min=90)
        await _add(db, env, "user", "My name is jitu", age_min=5)
        await _add(db, env, "assistant", "Nice to meet you, Jitu.", {"progress_id": "turn_newreply0001"}, age_min=5)
        await db.commit()
        stuck_id = stuck.id

    # The web app polls the stuck turn's status: it is settled as stopped (a newer reply exists), wherever it ran.
    async with env.factory() as db:
        out = await routes.chat_progress("turn_stuckold0001", env.user, db)
    assert out["done"] is True and out["status"] == "stopped"
    async with env.factory() as db:
        from app.models import ChatMessage

        row = await db.get(ChatMessage, stuck_id)
        assert row.meta["in_progress"] is False and row.meta["stopped"] is True and "Stopped" in row.content
    assert any("turn_stuckold0001" in k for k in env.redis.lists)  # its live feed was ended for open clients


@needs_db
async def test_loading_a_chat_settles_superseded_and_silent_turns_but_not_a_live_one(chat_db):
    from app.chat import routes
    from app.models import ChatMessage

    env = chat_db
    async with env.factory() as db:
        await _add(db, env, "user", "Hello", age_min=60)
        a = await _add(db, env, "assistant", "Chief is working on this…", {"in_progress": True, "progress_id": "turn_supersede01"}, age_min=60)
        await _add(db, env, "user", "I am gaurav", age_min=1)
        b = await _add(db, env, "assistant", "Chief is working on this…", {"in_progress": True, "progress_id": "turn_livenow0001"}, age_min=0)
        await db.commit()
    async with env.factory() as db:
        loaded = await routes.session_messages(env.sess_id, env.user, db)
    metas = [m["meta"] for m in loaded["messages"] if m["role"] == "assistant"]
    assert metas[0]["stopped"] is True and metas[0]["in_progress"] is False  # superseded by a newer reply
    assert metas[1]["in_progress"] is True  # the latest turn just started: left running
    async with env.factory() as db:
        live = await routes.chat_progress("turn_livenow0001", env.user, db)
    assert live["done"] is False and live["status"] == "running"

    # The same latest turn, silent for longer than the dead-turn limit, is settled too.
    async with env.factory() as db:
        row = await db.get(ChatMessage, b.id)
        row.created_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        await db.commit()
    async with env.factory() as db:
        loaded = await routes.session_messages(env.sess_id, env.user, db)
    assert [m["meta"].get("in_progress") for m in loaded["messages"] if m["role"] == "assistant"] == [False, False]
    assert a.id < b.id


@needs_db
async def test_status_is_found_for_a_turn_of_any_age(chat_db):
    from app.chat import routes

    env = chat_db
    async with env.factory() as db:
        await _add(db, env, "assistant", "Old answer", {"progress_id": "turn_veryold00001"}, age_min=60 * 24 * 30)
        for n in range(60):  # more than the 40 recent replies the old lookup scanned
            await _add(db, env, "assistant", f"reply {n}", {"progress_id": f"turn_r{n:010d}"})
        await db.commit()
    async with env.factory() as db:
        out = await routes.chat_progress("turn_veryold00001", env.user, db)
    assert out["done"] is True and out["status"] == "finished"
