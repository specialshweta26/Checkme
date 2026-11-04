"""Working, episodic, semantic, and procedural memory without Neo4j or a live model."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agent.memory import _assemble_memory
from app.memory_engine.episodes import Episode, normalize_episodes
from app.memory_engine.procedures import add_version
from app.memory_engine.semantic import chunk_text, graph_configured
from app.memory_engine.working import _merge, load_working, save_working


class _Result:
    def scalar_one_or_none(self):
        return None


class _Session:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def execute(self, *_a, **_k):
        return _Result()

    async def commit(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False


class _Redis:
    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


def test_assembled_memory_keeps_the_full_fact():
    fact = SimpleNamespace(text="rent " + ("x" * 3000), scope="agent", label="fact")
    text = _assemble_memory([], [fact])
    assert "x" * 3000 in text
    assert len(text) > 2400


def test_episode_normalizer_drops_secrets_and_keeps_failures():
    raw = [
        SimpleNamespace(content=Episode(observation="login failed", thoughts="", action="retried", result="still failed", feedback="", outcome="failed")),
        {"observation": "the password is hunter2", "action": "store it", "result": "", "feedback": "", "outcome": ""},
        {"observation": "", "action": "", "result": "", "feedback": "", "outcome": ""},
    ]
    rows = normalize_episodes(raw)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "failed"
    assert rows[0]["observation"] == "login failed"


def test_graph_stays_off_without_a_database(monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "neo4j_uri", "", raising=False)
    monkeypatch.setattr(settings, "neo4j_password", "", raising=False)
    assert graph_configured() is False


def test_documents_are_indexed_in_pages_and_kept_whole():
    body = "p" * 4500
    pages = chunk_text(body)
    assert "".join(pages) == body
    assert len(pages) == 3


@pytest.mark.asyncio
async def test_working_memory_roundtrip_merges_browser_and_progress():
    redis = _Redis()
    factory = lambda: _Session()  # noqa: E731
    user_id = uuid4()
    await save_working(user_id, "chat:1", browser={"url": "https://example.com"}, redis=redis, session_factory=factory)
    await save_working(user_id, "chat:1", progress={"step_index": 2, "skill_name": "Close the books"}, redis=redis, session_factory=factory)
    loaded = await load_working(user_id, "chat:1", redis=redis, session_factory=factory)
    assert loaded["browser"]["url"] == "https://example.com"
    assert loaded["progress"]["step_index"] == 2
    assert loaded["progress"]["skill_name"] == "Close the books"


def test_merge_keeps_earlier_browser_when_only_progress_arrives():
    merged = _merge({"browser": {"url": "https://example.com"}, "progress": {}, "context": {}, "variables": {}}, progress={"step_index": 1})
    assert merged["browser"]["url"] == "https://example.com"
    assert merged["progress"]["step_index"] == 1


def test_skill_revision_bumps_and_keeps_the_previous_instruction():
    session = _Session()
    row = SimpleNamespace(id=uuid4(), user_id=uuid4(), revision=1, name="Close the books", kind="saved", steps=["export"], instruction="Export the sheet.")
    first = add_version(session, row, bump=False)
    second = add_version(session, row, bump=True)
    row.instruction = "Export the sheet, then mail it."
    third = add_version(session, row, bump=True)
    assert (first, second, third) == (1, 2, 3)
    assert session.added[0].instruction == "Export the sheet."
    assert session.added[2].instruction == "Export the sheet, then mail it."
    assert session.added[0].revision == 1 and session.added[2].revision == 3
