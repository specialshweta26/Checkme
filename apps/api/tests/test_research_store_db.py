"""DbResearchStore against a real Postgres + pgvector. Runs only when RESEARCH_TEST_DB_URL is set (asyncpg URL)."""

from __future__ import annotations

import os
import uuid

import pytest

DB_URL = os.environ.get("RESEARCH_TEST_DB_URL", "")
pytestmark = pytest.mark.skipif(not DB_URL, reason="set RESEARCH_TEST_DB_URL to run against Postgres + pgvector")


async def test_db_store_isolation_freshness_and_search():
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.agent.research_store import DbResearchStore, _dims
    from app.models import Base, ResearchPage, ResearchSummary, User

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[User.__table__, ResearchPage.__table__, ResearchSummary.__table__]))
    sf = async_sessionmaker(engine, expire_on_commit=False)
    a, b = uuid.uuid4(), uuid.uuid4()
    async with sf() as db:
        db.add_all([User(id=a, email="a@x.test", primary_provider="google"), User(id=b, email="b@x.test", primary_provider="google")])
        await db.commit()
    dims = _dims()
    v1 = [1.0] + [0.0] * (dims - 1)
    v2 = [0.0, 1.0] + [0.0] * (dims - 2)
    sa, sb = DbResearchStore(a, sf), DbResearchStore(b, sf)
    await sa.put_pages([
        {"canonical_url": "https://d.test/sync", "url": "https://d.test/sync", "title": "Sync", "text": "semaphore text",
         "note": "Semaphore counter notes", "links": [["https://d.test/x", "X"]], "embedding": v1},
        {"canonical_url": "https://d.test/cats", "url": "https://d.test/cats", "title": "Cats", "text": "cats", "note": "Cats", "embedding": v2},
    ])
    # Upsert keeps one row per (user, url).
    await sa.put_pages([{"canonical_url": "https://d.test/sync", "url": "https://d.test/sync", "title": "Sync v2", "text": "t", "note": "Semaphore v2", "embedding": v1}])
    await sb.put_pages([{"canonical_url": "https://d.test/sync", "url": "https://d.test/sync", "title": "B's", "text": "b", "note": "b note"}])

    fresh = await sa.fresh_pages(["https://d.test/sync", "https://d.test/none"], 72)
    assert list(fresh) == ["https://d.test/sync"] and fresh["https://d.test/sync"]["title"] == "Sync v2"
    assert fresh["https://d.test/sync"]["from_memory"]
    async with sf() as db:
        await db.execute(text("UPDATE research_pages SET fetched_at = now() - interval '5 days' WHERE user_id = :u"), {"u": a})
        await db.commit()
    assert await sa.fresh_pages(["https://d.test/sync"], 72) == {}
    assert list(await sb.fresh_pages(["https://d.test/sync"], 72)) == ["https://d.test/sync"]  # b's row untouched

    await sa.save_summary({"crawl_id": "abc123", "goal": "semaphore", "summary": "Semaphores limit concurrency.", "embedding": v1, "sources": ["https://d.test/sync"]})
    hit = await sa.search("semaphore", v1, 5)
    assert hit["pages"][0]["url"] == "https://d.test/sync" and hit["pages"][0]["sim"] > 0.99
    assert hit["summaries"][0]["crawl_id"] == "abc123"
    kw = await sa.search("semaphore", None, 5)
    assert kw["pages"] and kw["summaries"]
    other = await sb.search("semaphore", v1, 5)
    assert other["summaries"] == [] and all(p.get("title") != "Sync v2" for p in other["pages"])
    async with sf() as db:
        await db.execute(text("DELETE FROM users WHERE id IN (:a, :b)"), {"a": a, "b": b})
        await db.commit()
    await engine.dispose()
