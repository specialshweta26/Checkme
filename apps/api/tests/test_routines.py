"""Routines (docs/ROUTINES.md): every scheduled run is recorded, and its result surfaces the same way on every device."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models import ChatSession, ScheduledRun, ScheduledTask, User, WorkspaceAgent
from app.tasks import service


def _user():
    return User(id=uuid.uuid4(), email="u@example.com", display_name="U", primary_provider="dev", timezone="UTC")


def _task(user, agent, session=None):
    return ScheduledTask(
        id=uuid.uuid4(), user_id=user.id, agent_id=agent.id, title="Morning brief", instruction="Brief me", prompt="brief me",
        schedule_kind="daily", schedule_label="every day at 9", next_run_at=datetime.now(timezone.utc), status="active",
        connected_apps=[], run_count=0, chat_session_id=session.id if session else None,
    )


class _DB:
    def __init__(self, agent, session):
        self.added = []
        self.agent, self.session = agent, session

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        return None

    async def flush(self):
        return None

    async def get(self, model, ident):
        if model is WorkspaceAgent and ident == self.agent.id:
            return self.agent
        if model is ChatSession and self.session is not None and ident == self.session.id:
            return self.session
        return None

    def runs(self):
        return [o for o in self.added if isinstance(o, ScheduledRun)]


@pytest.fixture
def setup(monkeypatch):
    user = _user()
    agent = WorkspaceAgent(id=uuid.uuid4(), user_id=user.id, name="Chief", color="#f60", unread=False)
    old = datetime.now(timezone.utc) - timedelta(days=3)
    session = ChatSession(id=uuid.uuid4(), user_id=user.id, title="Chief chat", state={}, agent_id=agent.id, updated_at=old)

    async def no_apps(*_a, **_k):
        return []

    monkeypatch.setattr("app.tasks.service.apps_from_used", no_apps)
    return user, agent, session, old


async def test_a_successful_run_is_recorded_and_surfaces_everywhere(setup, monkeypatch):
    user, agent, session, old = setup

    async def ok(*_a, **_k):
        return {"text": "Here is your brief.", "used_tools": []}

    monkeypatch.setattr("app.tasks.service.run_deep_agent", ok)
    db = _DB(agent, session)
    await service.execute_task(db, user, _task(user, agent, session), from_scheduler=True)

    [run] = db.runs()
    assert run.status == "succeeded" and run.trigger == "schedule"
    assert run.session_id == session.id and run.agent_id == agent.id
    assert run.result == "Here is your brief." and run.finished_at is not None
    # The result lands at the top of the agent's conversations on every device, and the agent shows as unread.
    assert session.updated_at > old
    assert agent.unread is True


async def test_a_failed_run_is_recorded_with_its_error(setup, monkeypatch):
    user, agent, session, _ = setup

    async def boom(*_a, **_k):
        raise RuntimeError("Gmail token expired")

    monkeypatch.setattr("app.tasks.service.run_deep_agent", boom)
    db = _DB(agent, session)
    with pytest.raises(RuntimeError):
        await service.execute_task(db, user, _task(user, agent, session), from_scheduler=False)

    [run] = db.runs()
    assert run.status == "failed" and run.trigger == "manual"
    assert run.error == "Gmail token expired"
    assert "couldn’t complete" in run.result
    assert agent.unread is True


def test_a_run_waiting_for_approval_takes_its_jobs_outcome():
    run = ScheduledRun(status="needs_approval", error="")
    assert service._outcome(run, None, None) == ("needs_approval", "")
    assert service._outcome(run, "running", None) == ("needs_approval", "")
    assert service._outcome(run, "done", None) == ("succeeded", "")
    assert service._outcome(run, "failed", "push rejected") == ("failed", "push rejected")
    assert service._outcome(run, "cancelled", "") == ("failed", "The approved work was cancelled.")
    assert service._outcome(ScheduledRun(status="failed", error="x"), "done", None) == ("failed", "x")
