"""End-to-end job flow against a real Postgres (claim with SKIP LOCKED, approvals, chat post-back).

Skipped unless CHECKME_TEST_DATABASE_URL is set, e.g.
  CHECKME_TEST_DATABASE_URL=postgresql+asyncpg://assistant:assistant@localhost:5432/assistant_test uv run pytest -q tests/test_jobs_db.py
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import select, text

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")


@pytest.fixture
async def factory():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import _migrate_jobs
    from app.models import Base

    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_jobs(conn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _user_and_agent(factory, name="Coder"):
    from app.models import User, WorkspaceAgent

    async with factory() as db:
        user = User(email=f"{uuid.uuid4().hex[:8]}@x.test", primary_provider="google")
        db.add(user)
        await db.flush()
        agent = WorkspaceAgent(user_id=user.id, name=name, focus="coding", kind="custom")
        db.add(agent)
        await db.commit()
        return user, agent


async def test_job_runs_waits_for_approval_and_posts_to_chat(factory):
    from app.agent.confirm import sign_confirm
    from app.jobs import service, worker
    from app.jobs.runner import run_job
    from app.models import AgentJob, ChatMessage, JobEvent, WorkspaceAgent

    user, agent = await _user_and_agent(factory)
    async with factory() as db:
        job = await service.create_job(db, user, agent=agent, prompt="Fix a/b", role="coder")
        await db.commit()
        job_id = job.id

    picked = await worker.claim(factory, 4, set())
    assert [p[0] for p in picked if p[1] == user.id] == [job_id]
    calls = []
    token = sign_confirm("tool", {"slug": "coding", "tool": "push_branch", "args": {"repo": "a/b", "branch": "agent/x"}}, ttl=3600)

    async def agent_impl(db, user_, session_id, request_id, history, query, confirmations, **kw):
        calls.append({"query": query, "history": list(history), "confirmations": confirmations, "auto_confirm": kw["auto_confirm"], "job": kw["job"]})
        await kw["progress"].emit("Cloning a/b", kind="tool", tool="git_checkout")
        if not confirmations:
            return {"text": "Pushed? Waiting for approval.", "pending_confirms": [{"token": token, "title": "Push agent/x"}]}
        await kw["progress"].record("pr", "Pull request: https://github.com/a/b/pull/1", {"url": "https://github.com/a/b/pull/1"})
        return {"text": "Opened https://github.com/a/b/pull/1"}

    assert await run_job(job_id, factory, agent_impl) == "waiting_approval"
    assert calls[0]["auto_confirm"] is False and "[Background job" in calls[0]["query"]

    async with factory() as db:
        job = await db.get(AgentJob, job_id)
        assert job.session_id is not None  # a session was created for it
        events = (await service.tree(db, user, job.root_id))["events"]
        assert [e["kind"] for e in events] == ["status", "status", "step", "message", "approval"]
        assert (await db.get(WorkspaceAgent, agent.id)).unread is True
        for ev in service.approve(job, token):
            db.add(ev)
        await db.commit()

    assert await worker.claim(factory, 4, set())
    assert await run_job(job_id, factory, agent_impl) == "done"
    assert calls[1]["confirmations"][0]["tool"] == "push_branch"
    assert "approved" in calls[1]["query"]
    assert len(calls[1]["history"]) == 2 and calls[0]["history"] == []  # first turn kept

    async with factory() as db:
        job = await db.get(AgentJob, job_id)
        assert job.status == "done" and "pull/1" in job.result
        msgs = (await db.execute(select(ChatMessage).where(ChatMessage.session_id == job.session_id))).scalars().all()
        assert msgs[-1].meta["job"]["status"] == "done" and "pull/1" in msgs[-1].content
        kinds = [e.kind for e in (await db.execute(select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.id))).scalars()]
        assert "pr" in kinds and kinds[-1] == "result"


async def test_one_running_job_per_user_and_stale_recovery(factory):
    from datetime import datetime, timedelta, timezone

    from app.jobs import service, worker
    from app.models import AgentJob

    user, agent = await _user_and_agent(factory)
    other, other_agent = await _user_and_agent(factory)
    async with factory() as db:
        a = await service.create_job(db, user, agent=agent, prompt="one")
        b = await service.create_job(db, user, agent=agent, prompt="two")
        c = await service.create_job(db, other, agent=other_agent, prompt="three")
        await db.commit()
    picked = {p[0] for p in await worker.claim(factory, 4, set())}
    assert a.id in picked and c.id in picked and b.id not in picked
    assert b.id not in {p[0] for p in await worker.claim(factory, 4, set())}  # user still has a running job

    async with factory() as db:
        row = await db.get(AgentJob, a.id)
        row.heartbeat_at = datetime.now(timezone.utc) - timedelta(seconds=worker.STALE_S + 30)
        await db.commit()
    assert await worker.recover_stale(factory) >= 1
    async with factory() as db:
        assert (await db.get(AgentJob, a.id)).status == "queued"
        for jid in (a.id, b.id, c.id):
            for ev in service.cancel(await db.get(AgentJob, jid)):
                db.add(ev)
        await db.commit()


async def test_failure_marks_failed_and_tells_chat(factory):
    from app.jobs import service, worker
    from app.jobs.runner import run_job
    from app.models import AgentJob

    user, agent = await _user_and_agent(factory)
    async with factory() as db:
        job = await service.create_job(db, user, agent=agent, prompt="explode")
        await db.commit()
    await worker.claim(factory, 4, set())

    async def boom(*a, **kw):
        raise RuntimeError("sandbox unavailable")

    assert await run_job(job.id, factory, boom) == "failed"
    async with factory() as db:
        row = await db.get(AgentJob, job.id)
        assert "sandbox unavailable" in row.error
