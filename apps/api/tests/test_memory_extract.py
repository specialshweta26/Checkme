import uuid
from types import SimpleNamespace

import pytest

from app.agent import memory_extract
from app.agent.memory import SCOPE_SHARED
from app.models import MemoryExtractJob, MemoryPassage, User, WorkspaceAgent


class _FakeJobResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _DB:
    def __init__(self, user, agents):
        self.user = user
        self.agents = {a.id: a for a in agents}
        self.jobs = []
        self.added = []

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, MemoryExtractJob):
            self.jobs.append(obj)

    async def get(self, model, ident):
        if model is User:
            return self.user if self.user.id == ident else None
        if model is WorkspaceAgent:
            return self.agents.get(ident)
        return None

    async def execute(self, stmt, params=None):
        if self.jobs:
            return _FakeJobResult(self.jobs[0])
        return _FakeJobResult(None)


def _user():
    return SimpleNamespace(id=uuid.uuid4(), display_name="Ada", email="a@x.com", timezone="Asia/Kolkata")


def _agent(user, name="Home"):
    return SimpleNamespace(id=uuid.uuid4(), user_id=user.id, name=name, kind="custom")


def test_skip_turn_drops_acks_and_secrets():
    assert memory_extract._skip_turn("thanks", "you are welcome") is True
    assert memory_extract._skip_turn("ok", "done") is True
    assert memory_extract._skip_turn("remember my password is hunter2", "saved") is True
    assert memory_extract._skip_turn("I prefer markdown tables", "Got it.") is False


@pytest.mark.asyncio
async def test_apply_extract_writes_prefs_and_skips_secrets(monkeypatch):
    user = _user()
    agent = _agent(user)
    calls = []

    async def fake_read(*_a, **_k):
        return ""

    async def fake_upsert(_db, user_id, label, value, agent_id):
        calls.append(("block", label, value, agent_id))

    async def fake_remember(_db, user_id, kind, text, agent_id, scope=SCOPE_SHARED, **kwargs):
        calls.append(("fact", text, scope, agent_id))
        return MemoryPassage(user_id=user_id, agent_id=agent_id, kind=kind, text=text)

    monkeypatch.setattr(memory_extract, "read_core", fake_read)
    monkeypatch.setattr(memory_extract, "upsert_core", fake_upsert)
    monkeypatch.setattr(memory_extract, "remember", fake_remember)

    out = await memory_extract.apply_extract(
        None,
        user,
        agent,
        {
            "voice": "short answers",
            "prefs": "I prefer markdown tables",
            "facts": [
                {"text": "I live in Bengaluru", "scope": "user_shared"},
                {"text": "my password is hunter2", "scope": "agent"},
            ],
        },
    )
    assert out["applied"] is True
    assert out["prefs"] is True and out["voice"] is True
    assert any(item[0] == "block" and item[1] == "prefs" and "tables" in item[2] for item in calls)
    assert any(item[0] == "fact" and "Bengaluru" in item[1] and item[2] == SCOPE_SHARED for item in calls)
    assert not any("password" in str(item).lower() and item[0] == "fact" for item in calls)


@pytest.mark.asyncio
async def test_apply_extract_discards():
    out = await memory_extract.apply_extract(None, _user(), _agent(_user()), {"discard": True})
    assert out["applied"] is False


@pytest.mark.asyncio
async def test_enqueue_is_idempotent_per_request_and_agent():
    user = _user()
    agent = _agent(user)
    db = _DB(user, [agent])
    first = await memory_extract.enqueue_extract(db, user.id, agent.id, uuid.uuid4(), "req-1", user_text="hi")
    second = await memory_extract.enqueue_extract(db, user.id, agent.id, uuid.uuid4(), "req-1", user_text="hi")
    assert first is not None
    assert second is first
    assert len(db.jobs) == 1
