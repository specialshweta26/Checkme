"""Admin test scenarios (docs/ADMIN.md "Test scenarios"): everyday questions run through the agent as the admin,
one after another, each timed; reads only."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import text

from app.admin import scenarios as sc

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")


def test_the_default_questions_cover_every_kind_of_connector():
    groups = {s["group"] for s in sc.defaults()}
    assert {"Instant", "Data connectors", "Mail and calendar", "Web", "Several connectors", "Workspace"} <= groups
    ids = [s["id"] for s in sc.defaults()]
    assert len(ids) == len(set(ids)) >= 20
    banned = ("send ", "delete", "reply to", "book ", "pay ", "publish")  # reads only
    assert not [s for s in sc.defaults() if any(b in s["text"].lower() for b in banned)]


def test_a_plan_keeps_follow_ups_only_with_their_first_question_and_adds_your_own():
    picked = sc.plan(["data-followup", "web-fact"], ["  How many orders today?  ", "", "x" * 2000])
    assert [s["id"] for s in picked] == ["web-fact", "custom-1", "custom-2"]  # the follow-up lost its first question
    assert picked[1]["text"] == "How many orders today?" and len(picked[2]["text"]) == 1000
    both = [s["id"] for s in sc.plan(["data-trend", "data-followup"], [])]
    assert both == ["data-trend", "data-followup"]
    assert len(sc.plan([], [])) == len(sc.defaults())


@pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")
async def test_a_run_times_each_question_and_keeps_follow_ups_in_one_chat(monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.db as appdb
    from app.agent import deep
    from app.agent import meter
    from app.models import Base, ChatMessage, User

    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMPTZ"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(appdb, "SessionLocal", factory)

    async def never_over(db, user):
        return False

    monkeypatch.setattr(meter, "over_user_cap", never_over)
    seen: list[tuple[str, int]] = []

    async def fake_agent(db, user, session_id, request_id, history, query, confirmations, **kw):
        seen.append((query, len(history)))
        await asyncio.sleep(0.01)
        return {"text": f"answer to {query}", "timing": {"total_ms": 10, "llm_ms": 5, "llm_calls": 1, "tools": [], "calls": []}}

    monkeypatch.setattr(deep, "run_deep_agent", fake_agent)
    async with factory() as ses:
        user = User(email=f"scen-{uuid.uuid4().hex[:8]}@example.com", display_name="S", primary_provider="email")
        ses.add(user)
        await ses.commit()
    try:
        run = sc.start(user.id, ["smalltalk", "data-trend", "data-followup"], [])
        for _ in range(200):
            if run["status"] != "running":
                break
            await asyncio.sleep(0.05)
        assert run["status"] == "done", run
        rows = {r["id"]: r for r in run["results"]}
        assert all(r["status"] == "ok" and r["total_ms"] >= 0 for r in rows.values())
        assert rows["data-trend"]["session_id"] == rows["data-followup"]["session_id"]  # the follow-up shares the chat
        assert rows["smalltalk"]["session_id"] != rows["data-trend"]["session_id"]
        assert [q for q, _ in seen] == [sc.plan(["data-trend"], [])[0]["text"], "Same as before but only for the last 3 days."]
        assert seen[1][1] == 2  # the follow-up saw the first question and its answer
        async with factory() as ses:
            timed = (await ses.execute(select(ChatMessage).where(ChatMessage.user_id == user.id, ChatMessage.role == "assistant"))).scalars().all()
            assert len(timed) == 3 and all("timing" in m.meta and m.meta.get("scenario_run") == run["id"] for m in timed)
    finally:
        async with factory() as ses:
            await ses.execute(text("DELETE FROM chat_sessions WHERE user_id = :i"), {"i": user.id})
            await ses.execute(text("DELETE FROM users WHERE id = :i"), {"i": user.id})
            await ses.commit()
        await engine.dispose()
