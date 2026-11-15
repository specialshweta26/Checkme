"""The quality loop (docs/QUALITY.md): ratings on replies, the per-agent summary, and feedback → test cases."""

from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import text

from app.quality import service

# ---------------------------------------------------------------- no database


@pytest.mark.parametrize(("sim", "bucket"), [(1.0, "unchanged"), (0.97, "unchanged"), (0.8, "light"), (0.7, "light"), (0.4, "rewritten")])
def test_draft_buckets_match_the_usage_page(sim, bucket):
    assert service.draft_bucket(sim) == bucket


def test_feedback_cases_keep_checks_someone_filled_in(tmp_path, monkeypatch):
    from evals import from_feedback as ff

    monkeypatch.setattr(ff, "CASES", tmp_path / "feedback_cases.json")
    first = [{"name": "feedback a", "message": "list my downloads", "history": [], "why": "Incomplete", "must_call": [], "must_not_call": [], "reply_has": [], "reply_lacks": []}]
    ff.CASES.write_text(json.dumps(ff.merge([], first)))
    filled = json.loads(ff.CASES.read_text())
    filled[0]["must_call"] = ["list_dir"]
    ff.CASES.write_text(json.dumps(filled))
    # Re-importing the same thumbs-down doesn't wipe the check.
    again = ff.merge(ff.load_cases(ff.CASES), [dict(first[0], why="Incomplete; missed sales.csv")])
    assert again[0]["must_call"] == ["list_dir"] and "sales.csv" in again[0]["why"]
    ff.CASES.write_text(json.dumps(again + [{"name": "feedback b", "message": "x", "must_call": []}]))
    runnable = ff.scenarios()
    assert [s.name for s in runnable] == ["feedback a"]  # b has no checks yet, so it waits
    assert runnable[0].must_call == ["list_dir"] and runnable[0].note.startswith("From user feedback")


def test_rating_routes_validate(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.deps import current_user
    from app.quality import routes

    async def rate(db, user, **kw):
        if kw["rating"] not in (-1, 0, 1):
            raise service.QualityError("A rating is 1 (good), -1 (bad) or 0 (clear).")
        return {"rating": kw["rating"], "request_id": kw["request_id"], "agent_id": None}

    monkeypatch.setattr(service, "rate", rate)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    app.dependency_overrides[current_user] = lambda: object()
    from app.db import get_db

    app.dependency_overrides[get_db] = lambda: None
    http = TestClient(app)
    ok = http.post("/api/quality/feedback", json={"session_id": str(uuid.uuid4()), "request_id": "r1", "rating": -1, "reasons": ["wrong"]})
    assert ok.status_code == 200 and ok.json()["rating"] == -1
    bad = http.post("/api/quality/feedback", json={"session_id": str(uuid.uuid4()), "request_id": "r1", "rating": 5})
    assert bad.status_code == 400 and "rating" in bad.json()["detail"]


# ---------------------------------------------------------------- against a real database (CHECKME_TEST_DATABASE_URL)

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")


@pytest.fixture
async def db():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Base, ChatMessage, ChatSession, User, WorkspaceAgent

    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    uid, other = uuid.uuid4(), uuid.uuid4()
    agent, sid, gid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add_all([User(id=uid, email=f"{uid}@test.local", primary_provider="google"), User(id=other, email=f"{other}@test.local", primary_provider="google")])
        await s.flush()
        s.add(WorkspaceAgent(id=agent, user_id=uid, name="Marki"))
        await s.flush()
        s.add(ChatSession(id=sid, user_id=uid, title="t", agent_id=agent, state={}))
        await s.flush()
        s.add_all(
            [
                ChatMessage(session_id=sid, user_id=uid, role="user", content="hello", request_id="r0"),
                ChatMessage(session_id=sid, user_id=uid, role="assistant", content="Hi!", request_id="r0", meta={"request_id": "r0", "agent_id": str(agent)}),
                ChatMessage(session_id=sid, user_id=uid, role="user", content="get Q3 revenue from the portal", request_id="r1"),
                ChatMessage(session_id=sid, user_id=uid, role="assistant", content="Q3 revenue was 9.1M.", request_id="r1",
                            meta={"request_id": "r1", "agent_id": str(agent), "used_tools": ["browser_task"]}),
            ]
        )  # fmt: skip
        await s.commit()
    yield factory, uid, other, agent, sid
    async with factory() as s:
        for u in (uid, other):
            await s.execute(text("DELETE FROM users WHERE id = :id"), {"id": u})
        await s.execute(text("DELETE FROM chat_sessions WHERE id = :id"), {"id": sid})
        await s.commit()
    await engine.dispose()


@needs_db
async def test_rate_summarise_export_and_clear(db):
    from app.models import User

    factory, uid, other, agent, sid = db
    async with factory() as s:
        me, stranger = await s.get(User, uid), await s.get(User, other)
        # Someone else can't rate my conversation.
        with pytest.raises(service.QualityError):
            await service.rate(s, stranger, session_id=sid, request_id="r1", rating=-1)
        await service.rate(s, me, session_id=sid, request_id="r0", rating=1)
        out = await service.rate(s, me, session_id=sid, request_id="r1", rating=-1, reasons=["wrong", "made-up-reason"], note="It's 8.4M")
        assert out == {"rating": -1, "request_id": "r1", "agent_id": str(agent)}
        # Rating again changes the same row rather than adding one.
        await service.rate(s, me, session_id=sid, request_id="r1", rating=-1, reasons=["wrong"], note="It's 8.4M, see the export")
        ratings = await service.ratings_for(s, me, sid)
        assert ratings == {f"r0:{agent}": 1, f"r1:{agent}": -1}
        bad = await service.list_feedback(s, me, rating=-1)
        assert len(bad) == 1 and bad[0]["prompt"] == "get Q3 revenue from the portal" and bad[0]["tools"] == ["browser_task"]
        assert bad[0]["reasons"] == ["wrong"] and bad[0]["agent_name"] == "Marki"
        summary = await service.summary(s, me)
        assert (summary["up"], summary["down"], summary["satisfaction"]) == (1, 1, 50)
        assert summary["reasons"][0]["reason"] == "wrong" and summary["agents"][0]["agent_name"] == "Marki"
        cases = await service.export_cases(s, me)
        assert cases[0]["message"] == "get Q3 revenue from the portal" and cases[0]["history"][-1]["content"] == "Hi!"
        assert "8.4M" in cases[0]["why"] and cases[0]["bad_reply"] == "Q3 revenue was 9.1M."
        assert await service.list_feedback(s, stranger) == []
        await service.rate(s, me, session_id=sid, request_id="r1", rating=0)
        assert await service.ratings_for(s, me, sid) == {f"r0:{agent}": 1}
        with pytest.raises(service.QualityError, match="wasn't found"):
            await service.rate(s, me, session_id=sid, request_id="nope", rating=1)
