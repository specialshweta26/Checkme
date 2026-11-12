"""A background job that stops to wait for the user says so in the chat, and the user's reply reaches it."""

import uuid
from types import SimpleNamespace

import pytest

from app.chat import routes
from app.jobs import service


def _agent(name="Oms"):
    return SimpleNamespace(id=uuid.uuid4(), name=name, color="", title="", role="worker", instructions="")


def _waiting(kind="handoff"):
    job = service.new_job(uuid.uuid4(), agent=_agent(), prompt="download the report", session_id=uuid.uuid4())
    job.status = "running"
    pending = [{"token": "t1", "title": "Take control of the computer", "kind": kind}]
    service.apply_outcome(job, "q", {"text": "Please sign in on the open screen, then reply continue.", "pending_confirms": pending})
    return job


class FakeDB:
    def __init__(self):
        self.added = []

    def add(self, row):
        self.added.append(row)

    async def get(self, *a):
        return None


def test_the_waiting_message_is_kept_and_posted_with_how_to_reply():
    job = _waiting()
    assert job.status == "waiting_approval" and "sign in" in job.result
    db = FakeDB()
    import asyncio

    asyncio.run(service.post_to_chat(db, job))
    body = db.added[0].content
    assert "Please sign in" in body and "reply here" in body


def test_an_approval_wait_says_yes_or_no():
    job = _waiting(kind="send")
    db = FakeDB()
    import asyncio

    asyncio.run(service.post_to_chat(db, job))
    assert "Reply “yes”" in db.added[0].content


def test_finishing_a_handover_carries_on_without_running_it():
    job = _waiting()
    events = service.resume_after_handoff(job, "done, signed in")
    assert job.status == "queued" and job.meta["confirmations"] == [] and "signed in" in job.meta["next_query"]
    assert events and events[0].kind == "status"


def test_a_handover_resume_refuses_other_approvals():
    with pytest.raises(service.JobError):
        service.resume_after_handoff(_waiting(kind="send"), "done")


@pytest.fixture
def chat(monkeypatch):
    seen = {"stored": [], "stopped": 0}

    async def store(db, sess, user_id, request_id, text, reply, meta):
        seen["stored"].append(reply)

    async def last_event(db, job):
        return 0

    async def stop(db, user_id, session_id, agent_id=None, since=None):
        seen["stopped"] += 1
        return []

    monkeypatch.setattr(routes, "_store", store)
    monkeypatch.setattr(routes, "_last_event_id", last_event)
    monkeypatch.setattr(service, "stop_chat_jobs", stop)
    import app.jobs.worker as worker

    monkeypatch.setattr(worker, "wake", lambda: None)
    return seen


async def _reply(job, text):
    sess = SimpleNamespace(id=job.session_id)
    agent = _agent()
    return await routes._answer_waiting_job(FakeDB(), SimpleNamespace(id=job.user_id), sess, agent, job, text, "r1")


async def test_a_reply_after_a_handover_continues_the_job(chat):
    job = _waiting()
    out = await _reply(job, "continue")
    assert job.status == "queued" and "carrying on" in out["text"] and chat["stopped"] == 0


async def test_stop_or_no_ends_a_waiting_job(chat):
    for text in ("stop", "no"):
        job = _waiting()
        out = await _reply(job, text)
        assert "Stopped" in out["text"] and job.status == "waiting_approval"
    assert chat["stopped"] == 2
