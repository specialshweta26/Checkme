"""A turn whose server process stopped (a deploy) is reported within a minute as interrupted, with Continue, instead
of leaving the app waiting on it; on shutdown, turns in flight get time to finish first."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.agent import progress
from app.chat import routes


class FakeRedis:
    def __init__(self):
        self.data = {}

    async def set(self, key, value, ex=None):
        self.data[key] = value

    async def exists(self, key):
        return 1 if key in self.data else 0


async def test_a_running_turn_marks_itself_alive_and_stops_when_cancelled(monkeypatch):
    monkeypatch.setattr(progress, "ALIVE_EVERY_S", 0.01)
    r = FakeRedis()
    task = asyncio.create_task(progress.keep_alive("u1", "turn_abc", r))
    await asyncio.sleep(0.03)
    assert await progress.turn_alive("u1", "turn_abc", r) is True
    task.cancel()
    assert await progress.turn_alive("u1", "turn_other", r) is False


async def test_progress_reports_a_cut_off_turn_as_interrupted(monkeypatch):
    msg = SimpleNamespace(id=1, session_id="s1", content="Chief is working on this…",
                          meta={"progress_id": "turn_abc", "in_progress": True, "steps": []})
    created = datetime.now(timezone.utc) - timedelta(seconds=routes.INTERRUPTED_AFTER_S + 5)

    async def stored(db, user, pid):
        return ("running" if msg.meta.get("in_progress") else "stopped"), created, msg

    async def empty(*a, **k):
        return [] if not a or a[-1] != "session" else ""

    async def nothing(*a, **k):
        return ""

    async def no_later(db, m):
        return False

    async def gone(user_id, pid, redis_client=None):
        return False

    class Reporter:
        def __init__(self, *a, **k):
            pass

        async def finish(self):
            pass

    class Db:
        async def commit(self):
            pass

    monkeypatch.setattr(routes, "_stored_turn_status", stored)
    monkeypatch.setattr(routes, "read_progress", lambda *a, **k: asyncio.sleep(0, result=[{"kind": "step", "at": datetime.now().timestamp()}]))
    monkeypatch.setattr(routes, "read_progress_session", nothing)
    monkeypatch.setattr(routes, "read_progress_draft", nothing)
    monkeypatch.setattr(routes, "_has_later_reply", no_later)
    monkeypatch.setattr(progress, "turn_alive", gone)
    monkeypatch.setattr(routes, "ProgressReporter", Reporter)
    out = await routes.chat_progress("turn_abc", user=SimpleNamespace(id="u1"), db=Db())
    assert out["done"] and out["status"] == "stopped"
    assert msg.meta["interrupted"] and msg.meta["can_continue"] and "interrupted by an app update" in msg.content


async def test_a_young_turn_is_not_cut_off(monkeypatch):
    created = datetime.now(timezone.utc)
    msg = SimpleNamespace(id=1, session_id="s1", content="", meta={"progress_id": "turn_new", "in_progress": True})

    async def stored(db, user, pid):
        return "running", created, msg

    async def nothing(*a, **k):
        return ""

    async def no_later(db, m):
        return False

    async def gone(*a, **k):
        return False

    monkeypatch.setattr(routes, "_stored_turn_status", stored)
    monkeypatch.setattr(routes, "read_progress", lambda *a, **k: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(routes, "read_progress_session", nothing)
    monkeypatch.setattr(routes, "read_progress_draft", nothing)
    monkeypatch.setattr(routes, "_has_later_reply", no_later)
    monkeypatch.setattr(progress, "turn_alive", gone)
    out = await routes.chat_progress("turn_new", user=SimpleNamespace(id="u1"), db=None)
    assert out["status"] == "running" and msg.meta["in_progress"]  # just started: its alive mark may not be written yet


async def test_shutdown_lets_turns_finish_but_not_forever():
    async def quick():
        await asyncio.sleep(0.01)

    async def slow():
        await asyncio.sleep(10)

    routes.RUNNING_TURNS.clear()
    a, b = asyncio.create_task(quick()), asyncio.create_task(slow())
    routes.RUNNING_TURNS.update({a, b})
    assert await routes.finish_running_turns(0.1) == 1  # the quick one finished; the slow one is cut off
    b.cancel()
    routes.RUNNING_TURNS.clear()


def test_a_rows_id_is_read_without_the_database_after_a_rollback():
    import uuid as _uuid

    from sqlalchemy.orm import make_transient_to_detached
    from sqlalchemy.orm.attributes import instance_state

    from app.chat.routes import _pk
    from app.models import User

    row_id = _uuid.uuid4()
    user = User(id=row_id, email="a@example.com")
    make_transient_to_detached(user)
    state = instance_state(user)
    state._expire(state.dict, set())
    assert _pk(user) == row_id
