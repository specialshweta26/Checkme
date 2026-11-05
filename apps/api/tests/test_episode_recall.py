"""Episode recall searches all of a user's episodes by meaning and by keyword, not only the latest 40."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text

from app.memory_engine import recall

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")


def test_fuse_ranks_prefers_items_in_both_lists():
    assert recall.fuse_ranks(["a", "b", "c"], ["d", "b"])[0] == "b"


def test_fuse_ranks_keeps_order_and_limit():
    assert recall.fuse_ranks([1, 2, 3], [], limit=2) == [1, 2]
    assert recall.fuse_ranks([], []) == []


@pytest.fixture
async def factory():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Base

    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _vec(first: float, second: float) -> list[float]:
    from app.models import DIMS

    return [first, second] + [0.0] * (DIMS - 2)


NEAR, FAR = _vec(1.0, 0.0), _vec(0.0, 1.0)


async def _seed(factory, rows: list[dict]):
    from app.models import MemoryEpisode, User, WorkspaceAgent

    async with factory() as db:
        user = User(email=f"{uuid.uuid4().hex[:8]}@x.test", primary_provider="google")
        db.add(user)
        await db.flush()
        agent = WorkspaceAgent(user_id=user.id, name=f"A{uuid.uuid4().hex[:6]}")
        db.add(agent)
        await db.flush()
        for row in rows:
            db.add(MemoryEpisode(user_id=user.id, agent_id=agent.id, scope="agent", status="active", **row))
        await db.commit()
    return user.id, agent.id


async def _embed_near(text_):
    return NEAR


@needs_db
async def test_finds_an_old_keyword_match_past_the_latest_40(factory, monkeypatch):
    monkeypatch.setattr("app.llm.embed_one", _embed_near)
    rows = [{"observation": "renewed the Zephyr contract", "embedding": None}]
    rows += [{"observation": f"unrelated note {i}", "embedding": FAR} for i in range(60)]
    user_id, agent_id = await _seed(factory, rows)
    async with factory() as db:
        lines = await recall._episodes(db, user_id, agent_id, "zephyr renewal")
    assert any("Zephyr" in line for line in lines)
    assert not any("unrelated" in line for line in lines)


@needs_db
async def test_finds_a_match_by_meaning_with_no_shared_words(factory, monkeypatch):
    monkeypatch.setattr("app.llm.embed_one", _embed_near)
    user_id, agent_id = await _seed(factory, [
        {"observation": "booked flights to Pune", "embedding": NEAR},
        {"observation": "cleaned the inbox", "embedding": FAR},
    ])  # fmt: skip
    async with factory() as db:
        lines = await recall._episodes(db, user_id, agent_id, "travel plans")
    assert lines == ["- booked flights to Pune"]


@needs_db
async def test_keyword_matches_survive_an_embedding_failure(factory, monkeypatch):
    async def broken(text_):
        raise RuntimeError("embeddings down")

    monkeypatch.setattr("app.llm.embed_one", broken)
    user_id, agent_id = await _seed(factory, [{"observation": "paid the Zephyr invoice", "embedding": None}])
    async with factory() as db:
        lines = await recall._episodes(db, user_id, agent_id, "zephyr")
    assert lines == ["- paid the Zephyr invoice"]


@needs_db
async def test_other_users_episodes_stay_hidden(factory, monkeypatch):
    monkeypatch.setattr("app.llm.embed_one", _embed_near)
    await _seed(factory, [{"observation": "secret Zephyr plan", "embedding": NEAR}])
    user_id, agent_id = await _seed(factory, [{"observation": "my own note", "embedding": FAR}])
    async with factory() as db:
        assert await recall._episodes(db, user_id, agent_id, "zephyr") == []


@needs_db
async def test_a_short_query_returns_the_latest_three(factory):
    user_id, agent_id = await _seed(factory, [{"observation": f"note {i}", "embedding": None} for i in range(5)])
    async with factory() as db:
        lines = await recall._episodes(db, user_id, agent_id, "hi")
    assert lines == ["- note 4", "- note 3", "- note 2"]
