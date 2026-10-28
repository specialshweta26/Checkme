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


async def test_webhook_delivery_queues_one_agent_job_and_deduplicates(factory):
    from app.automations.service import accept_webhook, create_webhook
    from app.models import AgentJob

    user, agent = await _user_and_agent(factory, "Inbox Manager")
    async with factory() as db:
        hook = await create_webhook(
            db,
            user,
            name="New issue triage",
            trigger="issues",
            condition="Only process newly opened issues.",
            action="Summarize the issue and prepare a priority recommendation.",
            agent_id=agent.id,
            requires_approval=True,
        )
        created = await accept_webhook(
            db,
            hook["id"],
            secret=hook["secret"],
            event_name="issues",
            event_id="delivery-1",
            payload={"action": "opened", "issue": {"number": 42, "title": "Fix login"}},
        )
        assert created["accepted"] and not created["duplicate"]
        job_id = uuid.UUID(created["job_id"])
        job = await db.get(AgentJob, job_id)
        assert job is not None and job.kind == "event" and job.status == "queued"
        assert job.meta["requires_approval"] is True
        assert "Fix login" in job.prompt

    async with factory() as db:
        duplicate = await accept_webhook(
            db,
            hook["id"],
            secret=hook["secret"],
            event_name="issues",
            event_id="delivery-1",
            payload={"action": "opened", "issue": {"number": 42, "title": "Fix login"}},
        )
        assert duplicate["duplicate"] is True and duplicate["job_id"] == str(job_id)
        mismatch = await accept_webhook(
            db,
            hook["id"],
            secret=hook["secret"],
            event_name="push",
            event_id="delivery-2",
            payload={"ref": "main"},
        )
        assert mismatch["ignored"] is True


async def _drain(factory, agent_impl, user_id, limit=20):
    """Run queued jobs for one user until nothing is claimable."""
    from app.jobs import worker
    from app.jobs.runner import run_job

    seen = []
    for _ in range(limit):
        picked = [p for p in await worker.claim(factory, 4, set()) if p[1] == user_id]
        if not picked:
            break
        for job_id, _ in picked:
            seen.append((job_id, await run_job(job_id, factory, agent_impl)))
    return seen


async def test_planner_coder_reviewer_then_user_approves_merge(factory):
    from app.agent.deep import RunContext, delegate_job
    from app.jobs import service
    from app.models import AgentJob, ChatMessage, ChatSession

    user, agent = await _user_and_agent(factory)
    async with factory() as db:
        sess = ChatSession(user_id=user.id, title="t", state={}, agent_id=agent.id)
        db.add(sess)
        await db.flush()
        root = await service.create_job(db, user, agent=agent, prompt="Add /health to a/b", role="planner", session_id=sess.id)
        await db.commit()
    from app.agent.confirm import sign_confirm

    merge_token = sign_confirm("tool", {"slug": "composio-github", "tool": "merge_pull_request", "args": {"repo": "a/b", "number": 7, "head_sha": "a" * 40}}, ttl=3600)
    log = []

    async def agent_impl(db, user_, session_id, request_id, history, query, confirmations, **kw):
        job = kw["job"]
        log.append((job.role, job.depth, bool(confirmations)))
        ctx = RunContext(db=db, user=user_, connectors={}, agent=kw["workspace_agent"], job=job, progress=kw["progress"])
        if job.role == "planner":
            if confirmations:
                return {"text": "Merged PR #7 after your approval."}
            if "delegated jobs finished" not in query:
                await delegate_job(ctx, "coder", "Build /health on agent/health and open a PR")
                return {"text": "Plan ready; coder is building.", "jobs_started": ctx.jobs_started}
            if "reviewer" not in query:
                await delegate_job(ctx, "reviewer", "Review a/b#7")
                return {"text": "PR open; reviewer is checking.", "jobs_started": ctx.jobs_started}
            return {"text": "Review passed. Merge needs your approval.", "pending_confirms": [{"token": merge_token, "title": "Merge a/b#7"}]}
        if job.role == "coder":
            await kw["progress"].record("pr", "Pull request: https://github.com/a/b/pull/7", {"url": "https://github.com/a/b/pull/7"})
            return {"text": "Opened https://github.com/a/b/pull/7"}
        return {"text": "LGTM, commented on #7"}

    ran = await _drain(factory, agent_impl, user.id)
    assert [r for r, _, _ in log] == ["planner", "coder", "planner", "reviewer", "planner"]
    assert ran[-1][1] == "waiting_approval"
    async with factory() as db:
        top = await db.get(AgentJob, root.id)
        assert top.status == "waiting_approval"
        tree = await service.tree(db, user, root.id)
        assert {j["role"] for j in tree["jobs"]} == {"planner", "coder", "reviewer"}
        assert all(j["depth"] == 2 for j in tree["jobs"] if j["role"] != "planner")
        assert any(e["kind"] == "pr" for e in tree["events"])
        for ev in service.approve(top, merge_token):
            db.add(ev)
        await db.commit()
    ran = await _drain(factory, agent_impl, user.id)
    assert log[-1] == ("planner", 1, True)
    async with factory() as db:
        top = await db.get(AgentJob, root.id)
        assert top.status == "done" and "Merged" in top.result
        msgs = (await db.execute(select(ChatMessage).where(ChatMessage.session_id == sess.id))).scalars().all()
        assert len(msgs) == 1 and msgs[0].meta["job"]["id"] == str(root.id)  # only the root posts to chat


async def test_scheduled_git_push_waits_for_approval_then_resumes(factory, monkeypatch):
    from app.agent.confirm import sign_confirm
    from app.jobs import service
    from app.models import AgentJob, ChatMessage, ScheduledTask
    from app.tasks import service as tasks

    user, agent = await _user_and_agent(factory)
    token = sign_confirm("tool", {"slug": "coding", "tool": "push_branch", "args": {"repo": "a/b", "branch": "agent/deps"}}, ttl=3600)

    async def fake_run(db, user_, session_id, request_id, history, query, confirmations=None, auto_confirm=False, workspace_agent=None, **kw):
        assert auto_confirm is True
        return {"text": "Bumped deps; push is waiting.", "pending_confirms": [{"token": token, "title": "Push agent/deps to a/b"}], "used_tools": []}

    async def no_apps(*a, **k):
        return []

    monkeypatch.setattr(tasks, "run_deep_agent", fake_run)
    monkeypatch.setattr(tasks, "apps_from_used", no_apps)
    async with factory() as db:
        task = ScheduledTask(user_id=user.id, agent_id=agent.id, title="Deps", instruction="Bump deps in a/b", schedule_kind="once", status="active")
        db.add(task)
        await db.commit()
        u = await db.get(type(user), user.id)
        await tasks.execute_task(db, u, task, from_scheduler=False)
        job = (await db.execute(select(AgentJob).where(AgentJob.scheduled_task_id == task.id))).scalar_one()
        assert job.status == "waiting_approval" and job.kind == "scheduled"
        msg = (await db.execute(select(ChatMessage).where(ChatMessage.session_id == job.session_id, ChatMessage.role == "assistant"))).scalar_one()
        assert msg.meta["job"]["id"] == str(job.id) and "Waiting for your approval" in msg.content
        for ev in service.approve(job, token):
            db.add(ev)
        await db.commit()
        job_id = job.id

    seen = []

    async def resume(db, user_, session_id, request_id, history, query, confirmations, **kw):
        seen.append((kw["auto_confirm"], confirmations))
        return {"text": "Pushed and opened https://github.com/a/b/pull/12"}

    await _drain(factory, resume, user.id)
    assert seen and seen[0][0] is True and seen[0][1][0]["tool"] == "push_branch"
    async with factory() as db:
        assert (await db.get(AgentJob, job_id)).status == "done"


async def test_schedule_task_for_named_agent(factory):
    from app.agent.staff import schedule_task
    from app.models import ScheduledTask, User

    user, coder = await _user_and_agent(factory, name="Coder")
    async with factory() as db:
        u = await db.get(User, user.id)
        out = await schedule_task(db, u, {"prompt": "every day at 9am check CI on a/b", "agent_name": "coder"})
        assert out.get("created"), out
        row = (await db.execute(select(ScheduledTask).where(ScheduledTask.user_id == user.id))).scalar_one()
        assert row.agent_id == coder.id
        bad = await schedule_task(db, u, {"prompt": "tomorrow 9am x", "agent_name": "Nobody"})
        assert "No agent named" in bad["error"]
