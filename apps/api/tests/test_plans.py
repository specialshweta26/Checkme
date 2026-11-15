"""Plan cards (docs/PLANS.md): a background task waits with its plan until the user starts it."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.jobs import plans
from app.jobs import service as jobs


def _job():
    return jobs.new_job(user_id=uuid.uuid4(), agent=SimpleNamespace(id=uuid.uuid4(), name="Raaz"), prompt="Compare flights Delhi to Goa on 12 Dec",
                        title="Flights", role="worker", kind="chat", session_id=uuid.uuid4())  # fmt: skip


def test_steps_are_cleaned_and_capped():
    assert plans.clean_steps(["1. Open IndiGo", "- Open Air India", "  ", "Open IndiGo", "2024 fares report"]) == [
        "Open IndiGo", "Open Air India", "2024 fares report",
    ]  # fmt: skip
    assert len(plans.clean_steps([f"step {i}" for i in range(30)])) == plans.MAX_STEPS
    assert plans.clean_steps("a\nb") == ["a", "b"]


def test_a_planned_job_waits_then_runs_the_edited_plan():
    job = _job()
    ev = plans.propose(job, ["Open each airline", "Compare fares"], ["the browser"], [], {"usd": 0.04, "basis": 5, "days": 14})
    assert job.status == "planned" and ev.kind == "plan"
    out = jobs.serialize_job(job)
    assert out["plan"]["steps"] == ["Open each airline", "Compare fares"] and out["plan"]["state"] == "proposed"
    assert "planned" in jobs.ACTIVE  # the chat keeps showing it

    events = plans.approve(job, ["Open IndiGo and Air India", "Compare fares", "Pick the cheapest morning flight"])
    assert job.status == "queued"
    assert job.meta["plan"]["state"] == "approved" and job.meta["plan"]["edited"] is True
    assert "1. Open IndiGo and Air India" in job.prompt and "re-plan" in job.prompt
    assert [t["content"] for t in events[0].data["todos"]][-1] == "Pick the cheapest morning flight"
    with pytest.raises(plans.PlanError):
        plans.approve(job)  # already running


def test_an_empty_plan_is_refused():
    job = _job()
    plans.propose(job, ["Only step"], [], [], None)
    with pytest.raises(plans.PlanError):
        plans.approve(job, ["  ", ""])
    assert job.status == "planned"
    plans.approve(job)  # unedited
    assert job.meta["plan"]["edited"] is False


class _DB:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        pass


async def test_start_tools_plan_first_from_chat_but_not_inside_jobs(monkeypatch):
    from app.agent import deep

    woke = []
    monkeypatch.setattr("app.jobs.worker.wake", lambda: woke.append(1))

    async def no_estimate(db, user_id):
        return None

    monkeypatch.setattr(plans, "estimate", no_estimate)

    announced = []

    async def create_job(db, user, announce=True, **kw):
        announced.append(announce)
        job = jobs.new_job(user_id=user.id, **kw)
        db.add(job)
        return job

    monkeypatch.setattr(jobs, "create_job", create_job)
    user = SimpleNamespace(id=uuid.uuid4())
    agent = SimpleNamespace(id=uuid.uuid4(), name="Raaz")
    ctx = SimpleNamespace(db=_DB(), user=user, agent=agent, session_id=uuid.uuid4(), job=None, jobs_started=[])

    out = await deep.start_coding_job(ctx, "Compare flights", "Flights", role="worker", steps=["Open sites", "Compare"], tools=["the browser"])
    assert out["planned"] is True and woke == []
    job = next(o for o in ctx.db.added if isinstance(o, jobs.AgentJob))
    assert job.status == "planned" and job.meta["plan"]["tools"] == ["the browser"] and announced == [False]

    # No steps: starts straight away, as before.
    out = await deep.start_coding_job(ctx, "Quick fix", "Fix", steps=[])
    assert out["started"] is True and woke == [1] and announced == [False, True]


def test_start_plan_route(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.deps import current_user
    from app.jobs import routes

    job = _job()
    plans.propose(job, ["One", "Two"], [], [], None)

    async def owned(db, user, job_id):
        return job

    class DB:
        def add(self, obj):
            pass

        async def commit(self):
            pass

    monkeypatch.setattr(routes, "_owned", owned)
    monkeypatch.setattr(routes, "wake", lambda: None)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    app.dependency_overrides[current_user] = lambda: object()
    app.dependency_overrides[get_db] = lambda: DB()
    http = TestClient(app)
    bad = http.post(f"/api/jobs/{job.id}/plan", json={"steps": [" "]})
    assert bad.status_code == 400
    ok = http.post(f"/api/jobs/{job.id}/plan", json={"steps": ["One", "Two", "Three"]})
    assert ok.status_code == 200 and ok.json()["status"] == "queued" and ok.json()["plan"]["steps"][-1] == "Three"
    again = http.post(f"/api/jobs/{job.id}/plan", json={})
    assert again.status_code == 400


def test_only_a_clear_yes_or_no_reaches_a_waiting_plan():
    from app.chat.routes import _plan_reply

    assert _plan_reply("go ahead") and _plan_reply("yes") and _plan_reply("no") and _plan_reply("stop")
    assert not _plan_reply("actually what's the weather in Goa?")
