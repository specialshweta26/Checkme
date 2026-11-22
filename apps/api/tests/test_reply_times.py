"""Reply times (docs/ADMIN.md "Reply times"): each chat reply keeps where its time went, and the admin console adds it
up: replies over the target, median and 95th percentile, and what the slowest ones spent their time on."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.admin import latency

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")


def _timing(total_s: float, model_s: float = 5, calls: list | None = None) -> dict:
    return {"reply_ms": int(total_s * 1000), "total_ms": int(total_s * 1000) - 300, "llm_ms": int(model_s * 1000), "llm_calls": 4,
            "stages": {"setup": 120, "agent": int(total_s * 1000) - 500}, "tools": [{"tool": "call_tool", "ms": 30000, "ok": True},
            {"tool": "write_analytics_report", "ms": 2500, "ok": True}], "calls": calls or []}  # fmt: skip


def test_a_slow_reply_shows_which_part_took_its_time():
    slow = _timing(70, model_s=12, calls=[{"connector": "local_mcp", "tool": "execute", "first_ms": 20000, "pages": 19, "ms": 52000}])
    out = latency.summarize([(datetime.now(timezone.utc), "a@x.test", slow), (datetime.now(timezone.utc), "b@x.test", _timing(9))], 45)
    assert out["replies"] == 2 and out["over_target"] == 1 and out["max_ms"] == 70000
    top = out["slowest"][0]
    assert top["user"] == "a@x.test" and top["connector_ms"] == 52000 and top["model_ms"] == 12000
    assert top["tool_ms"] == 2500  # call_tool is counted once, as connector time
    assert top["slowest_calls"][0] == {"connector": "local_mcp", "tool": "execute", "ms": 52000, "first_ms": 20000, "pages": 19}
    assert out["over_target_split"] == {"model_ms": 12000, "connector_ms": 52000, "tool_ms": 2500}


def test_percentiles_and_untimed_replies():
    rows = [(datetime.now(timezone.utc), "", _timing(s)) for s in range(1, 101)]
    rows.append((datetime.now(timezone.utc), "", {}))  # a reply from before timings were saved
    out = latency.summarize(rows, 45)
    assert out["replies"] == 100 and out["p50_ms"] in (50000, 51000) and out["p95_ms"] in (95000, 96000)
    assert out["over_target"] == 55 and len(out["slowest"]) == 20
    assert latency.summarize([], 45)["p50_ms"] is None


@pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")
async def test_the_admin_endpoint_reads_stored_replies_without_their_text(monkeypatch):
    import httpx
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.admin import routes
    from app.config import get_settings
    from app.db import get_db
    from app.models import Base, ChatMessage, ChatSession, User

    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMPTZ"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tag = uuid.uuid4().hex[:8]
    async with factory() as ses:
        user = User(email=f"timed-{tag}@example.com", display_name="T", primary_provider="email")
        ses.add(user)
        await ses.flush()
        chat = ChatSession(user_id=user.id)
        ses.add(chat)
        await ses.flush()
        ses.add_all([
            ChatMessage(session_id=chat.id, user_id=user.id, role="user", content="secret question", meta={}),
            ChatMessage(session_id=chat.id, user_id=user.id, role="assistant", content="secret answer", meta={"timing": _timing(61)}),
            ChatMessage(session_id=chat.id, user_id=user.id, role="assistant", content="old reply", meta={}),
        ])  # fmt: skip
        await ses.commit()

    async def db():
        async with factory() as s:
            yield s

    monkeypatch.setattr(get_settings(), "reply_target_s", 45.0)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    app.dependency_overrides[get_db] = db
    app.dependency_overrides[routes.admin_user] = lambda: user
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/admin/latency?days=1")
        assert r.status_code == 200, r.text
        mine = [s for s in r.json()["slowest"] if s["user"] == user.email]
        assert len(mine) == 1 and mine[0]["total_ms"] == 61000 and "secret" not in r.text
    finally:
        async with factory() as ses:
            await ses.execute(text("DELETE FROM users WHERE id = :i"), {"i": user.id})
            await ses.commit()
        await engine.dispose()


def test_a_stopped_or_failed_turn_is_marked():
    stopped = {**_timing(300), "stopped": "no progress"}
    failed = {**_timing(80), "failed": True}
    out = latency.summarize([(datetime.now(timezone.utc), "", stopped), (datetime.now(timezone.utc), "", failed)], 45)
    assert [s["ended"] for s in out["slowest"]] == ["stopped (no progress)", "failed"]


def test_the_timing_so_far_of_a_turn_that_did_not_finish():
    import time

    from app.agent import cancel
    from app.chat import routes

    run = cancel.Run(key="k", user_id="u", started=time.time() - 12)
    run.timing = lambda: {"llm_ms": 4000, "calls": [{"connector": "c", "tool": "t", "ms": 7000}]}
    out = routes._timing_so_far(run, stopped="stopped")
    assert out["llm_ms"] == 4000 and out["stopped"] == "stopped" and 11500 < out["reply_ms"] < 14000
    run.timing = lambda: 1 / 0  # a broken callable never stops the stopped marker from being saved
    assert routes._timing_so_far(run, failed=True)["failed"] is True


def test_tools_and_unaccounted_time_are_broken_down():
    t = _timing(60, model_s=20, calls=[{"connector": "c", "tool": "execute", "ms": 10000}])
    t["tools"] = [{"tool": "call_tool", "ms": 10000}, {"tool": "aggregate_result", "ms": 3000}, {"tool": "aggregate_result", "ms": 2000},
                  {"tool": "search_tools", "ms": 4000}]  # fmt: skip
    b = latency.breakdown(t)
    assert b["tools"][0] == {"tool": "call_tool", "calls": 1, "ms": 10000}
    assert {"tool": "aggregate_result", "calls": 2, "ms": 5000} in b["tools"]
    assert b["tool_ms"] == 9000 and b["other_ms"] == 60000 - 20000 - 10000 - 9000 and b["route_ms"] == 300
