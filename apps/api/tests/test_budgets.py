"""Per-agent budgets and limits (docs/BUDGETS.md)."""

from __future__ import annotations

import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.agent import meter
from app.budgets import service as budgets

# ---------------------------------------------------------------- no database


def test_limits_are_validated():
    assert budgets.clean_limits({"daily_usd": "2", "monthly_usd": 30, "task_minutes": 45.4}) == {"daily_usd": 2.0, "monthly_usd": 30.0, "task_minutes": 45}
    assert budgets.clean_limits({"daily_usd": 0, "monthly_usd": None, "task_minutes": ""}) == {}  # 0 or empty clears it
    assert budgets.clean_limits({"daily_tokens": "250000"}) == {"daily_tokens": 250000}
    for bad in ({"daily_usd": "lots"}, {"daily_usd": 50, "monthly_usd": 10}, {"task_minutes": 99999}):
        with pytest.raises(ValueError):
            budgets.clean_limits(bad)
    assert budgets.limits(SimpleNamespace(limits={"daily_usd": -1, "junk": 1})) == {}


def test_use_is_counted_per_agent_and_a_teammate_counts_as_itself():
    uid, raaz, marki = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        with meter.metered(uid, "chat"):
            with meter.as_agent(raaz):
                meter.record_tokens("m", 100, 10, 0)
                with meter.as_agent(marki):
                    meter.record_tokens("m", 5, 5, 0)
                meter.count("search")
            meter.record_tokens("m", 1, 1, 0)  # no agent: only the user meter
        mine = {(k[0], k[3]): v for k, v in meter._agent_pending.items() if k[1] == uid}
        assert mine[(raaz, "m")][1] == 100 and mine[(marki, "m")][1] == 5 and mine[(raaz, "search")][4] == 1
        assert len(mine) == 3
    finally:
        for k in [k for k in meter._agent_pending if k[1] == uid]:
            meter._agent_pending.pop(k)
        for k in [k for k in meter._pending if k[0] == uid]:
            meter._pending.pop(k)


def test_answers_to_a_budget_pause():
    from app.jobs import service as jobs

    job = jobs.new_job(uuid.uuid4(), agent=SimpleNamespace(id=uuid.uuid4(), name="Raaz"), prompt="p", title="t", role="worker", kind="chat")
    budgets.pause(job, budgets.BUDGET, "Raaz has reached its daily budget.")
    assert job.status == "waiting_input" and job.meta["question"]["options"] == [budgets.RUN_ANYWAY, budgets.LEAVE_IT]
    jobs.answer(job, budgets.RUN_ANYWAY)
    assert job.status == "queued" and budgets.may_overspend(job) and "question" not in job.meta

    job.status = "running"
    job.meta = {**job.meta, "limit_paused": True}
    budgets.pause(job, budgets.TIME_LIMIT, "Raaz has worked on this for 30 minutes.")
    jobs.answer(job, budgets.KEEP_GOING)
    assert job.status == "queued" and "Continue where you left off" in job.meta["next_query"] and "limit_paused" not in job.meta

    budgets.pause(job, budgets.TIME_LIMIT, "again")
    jobs.answer(job, budgets.STOP_IT)
    assert job.status == "cancelled"


# ---------------------------------------------------------------- against a real database (CHECKME_TEST_DATABASE_URL)

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")


@pytest.fixture
async def db(monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Base, User, WorkspaceAgent

    monkeypatch.setattr(meter, "cost_of", lambda model, inp, cached, out, table=None: (inp + out) / 1000)  # $1 per 1k tokens
    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        for col in ("accepts_from", "limits"):
            await conn.execute(text(f"ALTER TABLE workspace_agents ADD COLUMN IF NOT EXISTS {col} JSONB DEFAULT '{{}}'::jsonb"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    uid, raaz = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(User(id=uid, email=f"{uid}@test.local", primary_provider="google"))
        await s.flush()
        s.add(WorkspaceAgent(id=raaz, user_id=uid, name="Raaz", limits={"daily_usd": 2, "task_minutes": 30}))
        await s.commit()
    yield factory, uid, raaz
    async with factory() as s:
        await s.execute(text("DELETE FROM agent_usage_daily WHERE user_id = :u"), {"u": uid})
        await s.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
        await s.commit()
    await engine.dispose()


@needs_db
async def test_over_budget_blocks_chat_and_pauses_tasks(db, monkeypatch):
    from app.agent import deep
    from app.jobs import runner
    from app.jobs import service as jobs
    from app.models import User, WorkspaceAgent

    factory, uid, raaz_id = db
    async with factory() as s:
        user, raaz = await s.get(User, uid), await s.get(WorkspaceAgent, raaz_id)
        assert (await budgets.status(s, raaz, fresh=True))["over"] is None
        assert await deep._budget_block(s, raaz) is None

        with meter.metered(uid, "chat"), meter.as_agent(raaz_id):
            meter.record_tokens("m", 1500, 500, 0)  # $2.00
        assert await meter.flush(factory) >= 1  # stored, then read back
        st = await budgets.status(s, raaz, fresh=True)
        assert st["over"] == "daily" and st["today_usd"] == 2.0

        out = await deep._budget_block(s, raaz)
        assert "daily budget of $2.00" in out["text"] and "About panel" in out["text"]

        # A background task pauses before it runs, and asks.
        posted = []

        async def post(db, job, text=None):
            posted.append(job.id)

        monkeypatch.setattr(jobs, "post_to_chat", post)
        job = await jobs.create_job(s, user, agent=raaz, prompt="Compare quotes", title="Quotes", role="worker", kind="chat")
        job.status = "running"
        await s.commit()
        assert await runner._over_budget(s, job, raaz) == "waiting_input"
        assert job.meta["question"]["waiting_for"] == "budget" and posted == [job.id]
        jobs.answer(job, budgets.RUN_ANYWAY)
        await s.commit()
        job.status = "running"
        assert await runner._over_budget(s, job, raaz) is None  # the user said run anyway today

        # Mid-run: the watcher stops the run; the runner turns that stop into a pause, not a cancel.
        job.meta = {k: v for k, v in job.meta.items() if k != "budget_ok_day"}
        await s.commit()
        run = SimpleNamespace(cancelled=False, reason="")
        run.cancel = lambda reason="": (setattr(run, "cancelled", True), setattr(run, "reason", reason))
        await asyncio.wait_for(__import__("app.jobs.worker", fromlist=["x"])._watch_limits(factory, job.id, run, every=0.01), 5)
        assert run.reason.startswith("limit:budget:")
        assert await runner._stopped(s, job, run.reason) == "waiting_input"
        assert job.meta["limit_paused"] is True and job.meta["question"]["options"][0] == budgets.RUN_ANYWAY

        # Raising the budget lets it run again; a token limit works without prices.
        raaz.limits = {"daily_usd": 5}
        await s.commit()
        assert (await budgets.status(s, raaz, fresh=True))["over"] is None
        raaz.limits = {"daily_tokens": 1500}
        await s.commit()
        st = await budgets.status(s, raaz, fresh=True)
        assert st["over"] == "tokens" and st["today_tokens"] == 2000 and "1,500 tokens" in budgets.chat_reply(raaz, st)
        await s.execute(text("DELETE FROM agent_jobs WHERE user_id = :u"), {"u": uid})
        await s.commit()
