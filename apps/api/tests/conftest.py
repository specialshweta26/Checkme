"""Shared fixtures."""

from __future__ import annotations

import asyncio
import copy
import uuid

import pytest


@pytest.fixture(autouse=True)
def _offline_browsing_ladder(monkeypatch):
    """Tests never reach the reader, archive, Tavily Extract, hosted browsers or m. sites over the network.
    Ladder tests pass their own steps (``custom=``)."""
    from app.connectors import browsing

    async def _off(url, limit, state):
        return None

    for step in ("tavily", "remote", "alternates", "reader", "archive"):
        monkeypatch.setitem(browsing._STEPS, step, _off)
    browsing._CACHE.clear()
    from app.agent import web_tools

    web_tools._SEARCH_CACHE.clear()


class SkillStore:
    """In-memory stand-in for the skills persistence helpers (no database)."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.updates: list[tuple[str, dict]] = []

    async def create(self, user_id, agent_id, name, auto_named):
        sid = str(uuid.uuid4())
        self.rows[sid] = {
            "id": sid, "user_id": str(user_id), "agent_id": agent_id, "name": name, "steps": [], "transcript": [],
            "frames": [], "notes": [], "auto_named": auto_named, "status": "recording", "instruction": "", "error": "",
            "duration_ms": 0,
        }
        return sid

    async def update(self, skill_id, **fields):
        if skill_id not in self.rows:
            return False
        self.updates.append((skill_id, copy.deepcopy(fields)))
        self.rows[skill_id].update(copy.deepcopy(fields))
        return True

    async def get(self, skill_id):
        row = self.rows.get(skill_id)
        return copy.deepcopy(row) if row else None

    async def delete(self, skill_id):
        self.rows.pop(skill_id, None)

    async def find_recording(self, user_id):
        for sid, row in self.rows.items():
            if row["user_id"] == str(user_id) and row["status"] == "recording":
                return sid
        return None

    async def settle(self):
        from app.skills import service

        for _ in range(50):
            pending = [t for t in list(service._BACKGROUND) if not t.done()]
            if not pending:
                return
            await asyncio.wait(pending, timeout=2)


@pytest.fixture
def skill_store(monkeypatch):
    from app.skills import service

    store = SkillStore()
    monkeypatch.setattr(service, "_store_create", store.create)
    monkeypatch.setattr(service, "_store_update", store.update)
    monkeypatch.setattr(service, "_store_get", store.get)
    monkeypatch.setattr(service, "_store_delete", store.delete)
    monkeypatch.setattr(service, "_store_find_recording", store.find_recording)
    yield store
    for row in list(service._TEACH.values()):
        for name in ("auto_save", "flusher"):
            task = row.get(name)
            if task is not None:
                task.cancel()
    service._TEACH.clear()


class TaskStateStore:
    """In-memory stand-in for app.skills.task_state.DbStore."""

    def __init__(self):
        self.rows: dict[str, dict] = {}

    async def find_open(self, user_id, task_key):
        from app.skills.task_state import OPEN

        hits = [r for r in self.rows.values() if r["user_id"] == str(user_id) and r["task_key"] == task_key and r["status"] in OPEN]
        return copy.deepcopy(sorted(hits, key=lambda r: r["updated_at"])[-1]) if hits else None

    async def get(self, state_id):
        row = self.rows.get(str(state_id))
        return copy.deepcopy(row) if row else None

    async def create(self, data):
        from app.skills.task_state import _now

        sid = str(uuid.uuid4())
        row = {"parent_id": None, **copy.deepcopy(data), "id": sid, "created_at": _now(), "updated_at": _now()}
        self.rows[sid] = row
        return copy.deepcopy(row)

    async def update(self, state_id, **fields):
        from app.skills.task_state import _now

        row = self.rows.get(str(state_id))
        if row is None:
            return None
        row.update(copy.deepcopy(fields))
        row["updated_at"] = _now()
        return copy.deepcopy(row)

    async def list_for(self, user_id, session_id=None, job_id=None, limit=20):
        return [copy.deepcopy(r) for r in self.rows.values() if r["user_id"] == str(user_id)][:limit]


@pytest.fixture(autouse=True)
def task_store(monkeypatch):
    from app.skills import task_state

    store = TaskStateStore()
    monkeypatch.setattr(task_state, "STORE", store)
    return store
