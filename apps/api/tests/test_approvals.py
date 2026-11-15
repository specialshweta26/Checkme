"""One approvals queue (docs/APPROVALS.md): everything waiting on the user, across agents, decided in one place."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text

from app.agent.confirm import sign_confirm
from app.approvals import service

# ---------------------------------------------------------------- no database


def test_preview_lines_pick_readable_fields():
    assert service.preview_lines({"to": "ana@acme.com", "subject": "Q3 numbers", "body": "long…", "kind": "email"}) == [
        "To: ana@acme.com", "Subject: Q3 numbers",
    ]  # fmt: skip
    assert service.preview_lines({"repo": "a/b", "branch": "agent/fix"}) == ["Repo: a/b", "Branch: agent/fix"]
    assert service.preview_lines("Push agent/x to a/b") == ["Push agent/x to a/b"]
    assert service.preview_lines(None) == []


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
    factory = async_sessionmaker(engine, expire_on_commit=False)
    uid, other, raaz, marki = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add_all([User(id=uid, email=f"{uid}@test.local", primary_provider="google"), User(id=other, email=f"{other}@test.local", primary_provider="google")])
        await s.flush()
        s.add_all([WorkspaceAgent(id=raaz, user_id=uid, name="Raaz"), WorkspaceAgent(id=marki, user_id=uid, name="Marki")])
        await s.commit()
    yield factory, uid, other, raaz, marki
    async with factory() as s:
        for u in (uid, other):
            await s.execute(text("DELETE FROM users WHERE id = :id"), {"id": u})
        await s.commit()
    await engine.dispose()


@needs_db
async def test_queue_gathers_every_agent_and_batch_decides(db, monkeypatch):
    from app.jobs import plans
    from app.jobs import service as jobs
    from app.models import AgentJob, ChatMessage, ChatSession, User

    monkeypatch.setattr("app.jobs.worker.wake", lambda: None)
    factory, uid, other, raaz, marki = db
    async with factory() as s:
        me, stranger = await s.get(User, uid), await s.get(User, other)
        agents = {str(raaz): type("A", (), {"id": raaz, "name": "Raaz"})(), str(marki): type("A", (), {"id": marki, "name": "Marki"})()}
        # Raaz: a plan waiting. Marki: a paused send. Raaz: a question.
        plan_job = jobs.new_job(uid, agent=agents[str(raaz)], prompt="Compare flights", title="Flights", role="worker", kind="chat")
        plans.propose(plan_job, ["Open sites", "Compare"], ["the browser"], [], None)
        send_job = jobs.new_job(uid, agent=agents[str(marki)], prompt="Email the Q3 deck", title="Q3 deck", role="worker", kind="chat")
        token = sign_confirm("tool", {"slug": "gmail", "tool": "send", "args": {"to": "ana@acme.com"}}, ttl=3600)
        send_job.status = "waiting_approval"
        send_job.meta = {**send_job.meta, "pending": [{"token": token, "title": "Send the Q3 deck to Ana", "preview": {"to": "ana@acme.com", "subject": "Q3 deck"}}]}
        q_job = jobs.new_job(uid, agent=agents[str(raaz)], prompt="Merge?", title="PR 7", role="planner", kind="chat")
        q_job.status = "waiting_input"
        q_job.meta = {**q_job.meta, "question": {"question": "Merge PR 7?", "options": ["Yes, merge it", "No, leave it open"]}}
        # A chat reply with a live approval, and one with an expired one.
        sess = ChatSession(user_id=uid, title="t", agent_id=marki, state={})
        s.add_all([plan_job, send_job, q_job, sess])
        await s.flush()
        live = sign_confirm("tool", {"slug": "gmail", "tool": "send", "args": {}}, ttl=600)
        dead = sign_confirm("tool", {"slug": "gmail", "tool": "send", "args": {}}, ttl=-5)
        s.add(ChatMessage(session_id=sess.id, user_id=uid, role="assistant", content="Ready to send.", request_id="r1",
                          meta={"pending_confirms": [{"token": live, "title": "Send reply to Bo"}, {"token": dead, "title": "Old one"}]}))  # fmt: skip
        await s.commit()
        sess_id = sess.id

        plan_id, send_id, q_id = str(plan_job.id), str(send_job.id), str(q_job.id)
        out = await service.queue(s, me)
        kinds = sorted(i["kind"] for i in out["items"])
        assert kinds == ["action", "chat", "plan", "question"]  # the expired chat card is left out
        action = next(i for i in out["items"] if i["kind"] == "action")
        assert action["agent_name"] == "Marki" and action["lines"] == ["To: ana@acme.com", "Subject: Q3 deck"] and not action["expired"]
        plan = next(i for i in out["items"] if i["kind"] == "plan")
        assert plan["agent_name"] == "Raaz" and plan["lines"] == ["Open sites", "Compare"]
        assert (await service.queue(s, stranger))["count"] == 0  # nobody else sees them

        # Batch: start the plan, decline the send, and a question can't be batch-decided.
        res = await service.decide(s, me, [
            {"id": "p", "kind": "plan", "job_id": plan_id, "approve": True},
            {"id": "a", "kind": "action", "job_id": send_id, "token": token, "approve": False},
            {"id": "q", "kind": "question", "job_id": q_id, "approve": True},
            {"id": "x", "kind": "plan", "job_id": str(uuid.uuid4()), "approve": True},
        ])  # fmt: skip
        assert [r["ok"] for r in res] == [True, True, False, False]
        assert "open the chat" in res[2]["error"] and "no longer exists" in res[3]["error"]
        await s.refresh(plan_job)
        await s.refresh(send_job)
        assert plan_job.status == "queued" and send_job.status == "queued" and "declined" in send_job.meta["next_query"]
        # Someone else can't decide my jobs.
        stranger = type("U", (), {"id": other})()  # the batch rolled back failed items, expiring loaded rows
        res = await service.decide(s, stranger, [{"id": "q", "kind": "plan", "job_id": q_id, "approve": True}])
        assert res[0]["ok"] is False

        me = await s.get(User, uid)
        await service.answer(s, me, q_id, "Yes, merge it")
        await s.refresh(q_job)
        assert q_job.status == "queued"

        chat = next(i for i in (await service.queue(s, me))["items"] if i["kind"] == "chat")
        assert await service.dismiss_chat(s, stranger, chat["message_id"], live) is False
        assert await service.dismiss_chat(s, me, chat["message_id"], live) is True
        assert (await service.queue(s, me))["count"] == 0

        await s.execute(text("DELETE FROM agent_jobs WHERE user_id = :u"), {"u": uid})
        await s.execute(text("DELETE FROM chat_sessions WHERE id = :id"), {"id": sess_id})
        await s.commit()
