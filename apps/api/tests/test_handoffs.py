"""Agent-to-agent handoffs (docs/HANDOFFS.md): receipts, completion events, and who may send work to whom."""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from app.handoffs import service as handoffs

# ---------------------------------------------------------------- no database


def _agent(name, rule=None):
    return SimpleNamespace(id=uuid.uuid4(), name=name, accepts_from=rule or {})


def test_rules_default_to_anyone_and_refuse_with_a_reason():
    raaz, marki, sam = _agent("Raaz"), _agent("Marki"), _agent("Sam")
    assert handoffs.rule(raaz) == {"mode": "anyone", "agents": []}
    assert handoffs.refusal(raaz, marki) is None
    assert handoffs.refusal(raaz, None) is None  # the user can always message an agent
    marki.accepts_from = {"mode": "nobody"}
    assert "only takes work from the user" in handoffs.refusal(marki, raaz)
    assert handoffs.refusal(marki, marki) is None  # handing work to itself is fine
    marki.accepts_from = {"mode": "selected", "agents": [str(sam.id)]}
    assert handoffs.refusal(marki, sam) is None
    why = handoffs.refusal(marki, raaz, {str(sam.id): "Sam"})
    assert "only takes work from Sam and the user" in why
    assert handoffs.rule(_agent("X", {"mode": "weird"}))["mode"] == "anyone"


def test_clean_rule_only_lists_own_agents():
    own = {"a", "b", "self"}
    assert handoffs.clean_rule("selected", ["a", "zz", "self", "a"], own, "self") == {"mode": "selected", "agents": ["a"]}
    assert handoffs.clean_rule("nobody", ["a"], own, "self") == {"mode": "nobody", "agents": []}
    with pytest.raises(ValueError):
        handoffs.clean_rule("selected", ["zz"], own, "self")
    with pytest.raises(ValueError):
        handoffs.clean_rule("everyone", [], own, "self")


def test_receipt_lifecycle():
    db = SimpleNamespace(added=[], add=lambda o: db.added.append(o))
    row = handoffs.record(db, uuid.uuid4(), _agent("Raaz"), _agent("Marki"), "teammate", "Draft the reply")
    assert row.status == "sent" and row.from_name == "Raaz" and row.to_name == "Marki"
    handoffs.mark_received(row)
    assert row.status == "received" and row.received_at
    handoffs.mark_finished(row, "done", "Drafted. " * 400)
    assert row.status == "done" and len(row.reply) <= handoffs.REPLY_MAX and row.finished_at
    handoffs.mark_finished(row, "failed", "late")
    assert row.status == "done"  # a finished receipt stays as it was


# ---------------------------------------------------------------- against a real database (CHECKME_TEST_DATABASE_URL)

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")


@pytest.fixture
async def db():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Base, User, WorkspaceAgent

    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE workspace_agents ADD COLUMN IF NOT EXISTS accepts_from JSONB DEFAULT '{}'::jsonb"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    uid, raaz, marki, sam = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(User(id=uid, email=f"{uid}@test.local", primary_provider="google"))
        await s.flush()
        s.add_all([WorkspaceAgent(id=raaz, user_id=uid, name="Raaz"), WorkspaceAgent(id=marki, user_id=uid, name="Marki"),
                   WorkspaceAgent(id=sam, user_id=uid, name="Sam", accepts_from={"mode": "nobody"})])  # fmt: skip
        await s.commit()
    yield factory, uid, raaz, marki, sam
    async with factory() as s:
        await s.execute(text("DELETE FROM agent_messages WHERE user_id = :u"), {"u": uid})
        await s.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
        await s.commit()
    await engine.dispose()


@needs_db
async def test_delegate_writes_receipts_and_the_parent_gets_a_completion_event(db, monkeypatch):
    from app.agent import deep
    from app.jobs import service as jobs
    from app.models import AgentJob, AgentMessage, JobEvent, User, WorkspaceAgent

    factory, uid, raaz_id, marki_id, _sam_id = db
    async with factory() as s:
        user, raaz = await s.get(User, uid), await s.get(WorkspaceAgent, raaz_id)
        parent = await jobs.create_job(s, user, agent=raaz, prompt="Ship the fix", title="Fix", role="planner", kind="chat")
        await s.flush()
        parent.status = "running"
        ctx = SimpleNamespace(db=s, user=user, agent=raaz, job=parent, jobs_started=[], progress=None, task_state=None)

        # Sam takes work only from the user: refused, nothing queued, a refused receipt.
        out = await deep.delegate_job(ctx, "coder", "Write the patch", agent_name="Sam", title="Patch")
        assert out.get("refused") and "only takes work from the user" in out["error"] and ctx.jobs_started == []

        out = await deep.delegate_job(ctx, "reviewer", "Review PR 7", agent_name="Marki", title="Review PR 7")
        assert out["delegated"] is True
        child_id = uuid.UUID(out["job_id"])
        out = await deep.delegate_job(ctx, "coder", "Write the patch", title="Patch")  # to itself: no receipt
        assert out["delegated"] is True
        parent.status = "waiting_children"
        parent.meta = {**parent.meta, "children": [j["id"] for j in ctx.jobs_started]}
        await s.commit()

        rows = (await s.execute(select(AgentMessage).where(AgentMessage.user_id == uid).order_by(AgentMessage.created_at))).scalars().all()
        assert [(r.to_name, r.status) for r in rows] == [("Sam", "refused"), ("Marki", "sent")]

        await handoffs.job_started(s, child_id)
        await s.commit()
        child = await s.get(AgentJob, child_id)
        await s.refresh(rows[1])
        assert rows[1].status == "received" and rows[1].received_at

        child.status, child.result = "done", "Approved with two nits."
        await jobs.after_terminal(s, child)
        await s.commit()
        await s.refresh(rows[1])
        assert rows[1].status == "done" and rows[1].reply == "Approved with two nits."
        ev = (await s.execute(select(JobEvent).where(JobEvent.job_id == parent.id, JobEvent.kind == "handoff"))).scalars().one()
        assert ev.text == "Marki finished: Review PR 7" and ev.data["receipt"]["status"] == "done"

        mine = await handoffs.history(s, uid, agent_id=marki_id)
        assert [m["status"] for m in mine] == ["done"] and mine[0]["from_name"] == "Raaz"
        assert len(await handoffs.history(s, uid)) == 2
        assert await handoffs.history(s, uuid.uuid4()) == []  # nobody else sees them

        await s.execute(text("DELETE FROM agent_jobs WHERE user_id = :u"), {"u": uid})
        await s.commit()


@needs_db
async def test_ask_teammate_respects_the_rule_and_closes_its_receipt(db, monkeypatch):
    from app.agent import deep
    from app.models import AgentMessage, ChatSession, User, WorkspaceAgent

    factory, uid, raaz_id, marki_id, sam_id = db
    calls = []

    async def fake_run(*a, **kw):
        calls.append(kw["workspace_agent"].name)
        return {"text": "Hi, I'm Marki. I write the copy."}

    monkeypatch.setattr(deep, "run_deep_agent", fake_run)
    async with factory() as s:
        user = await s.get(User, uid)
        raaz, marki, sam = [await s.get(WorkspaceAgent, i) for i in (raaz_id, marki_id, sam_id)]
        sess = ChatSession(user_id=uid, title="g", state={})
        s.add(sess)
        await s.flush()
        ctx = SimpleNamespace(teammate_depth=0, teammates=[raaz, marki, sam], agent=raaz, db=s, user=user, session_id=sess.id,
                              group=SimpleNamespace(name="Launch"), query="intro", task_state=None, request_id="r", auto_confirm=False,
                              untrusted_seen=False, progress=None, used_tools=[], attachments=[], pending=[], jobs_started=[],
                              question=None, results=[], thread=[], workspace_changed=False)  # fmt: skip
        out = await deep._ask_teammate(ctx, str(sam.id), "Say hi")
        assert out.get("refused") and calls == []
        out = await deep._ask_teammate(ctx, str(marki.id), "Say hi")
        assert out["reply"].startswith("Hi, I'm Marki") and calls == ["Marki"]
        await s.commit()
        rows = (await s.execute(select(AgentMessage).where(AgentMessage.user_id == uid).order_by(AgentMessage.created_at))).scalars().all()
        assert [(r.to_name, r.channel, r.status) for r in rows] == [("Sam", "teammate", "refused"), ("Marki", "teammate", "done")]
        assert rows[1].reply.startswith("Hi, I'm Marki")
        await s.execute(text("DELETE FROM chat_sessions WHERE id = :id"), {"id": sess.id})
        await s.commit()
