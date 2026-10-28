"""A live chat turn is durable while an agent is still working."""

import uuid
from types import SimpleNamespace

import pytest

from app.chat.routes import _start_turn, _store
from app.models import ChatMessage


class _DB:
    def __init__(self):
        self.rows = []
        self.commits = 0

    def add(self, row):
        self.rows.append(row)

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_turn_and_working_marker_are_persisted_then_updated_in_place():
    db = _DB()
    user_id = uuid.uuid4()
    agent = SimpleNamespace(id=uuid.uuid4(), name="Researcher")
    group = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(id=uuid.uuid4(), title="", updated_at=None)

    working = await _start_turn(
        db,
        session,
        user_id,
        "request-1",
        "Compare the latest reports",
        progress_id="turn_12345678",
        agent=agent,
        group=group,
    )

    assert db.commits == 1
    assert [row.role for row in db.rows] == ["user", "assistant"]
    assert working.meta["in_progress"] is True
    assert working.meta["progress_id"] == "turn_12345678"
    assert working.meta["group_id"] == str(group.id)

    await _store(
        db,
        session,
        user_id,
        "request-1",
        "Compare the latest reports",
        "Here is the comparison.",
        {"used_tools": ["search_reports"]},
        working_message=working,
        thread=[{"text": "I checked the financial report.", "agent_name": "Finance"}],
    )

    assert [row.role for row in db.rows] == ["user", "assistant", "assistant"]
    assert working.content == "Here is the comparison."
    assert working.meta["in_progress"] is False
    assert working.meta["used_tools"] == ["search_reports"]
    assert isinstance(db.rows[-1], ChatMessage)
    assert db.commits == 2
