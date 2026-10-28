"""Shared fixtures."""

from __future__ import annotations

import asyncio
import copy
import uuid

import pytest


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
