"""Memory and routines belong to one workspace agent."""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.agent import memory
from app.agent.deep import RunContext, run_staff
from app.agent.staff import dispatch
from app.agents.scope import (
    COPY_CHIEF_LETTA_SQL,
    INSERT_MISSING_CHIEF_SQL,
    PROMOTE_NAMED_CHIEF_SQL,
    SCOPED_TABLES,
    assign_missing,
    backfill_statements,
    choose_default_agent,
    owner_by_user,
)
from app.models import ChatMessage, ChatSession, MemoryBlock, MemoryPassage, ScheduledTask, User, WorkspaceAgent
from app.tasks.service import create_from_prompt, execute_task


class _Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        if not self._rows:
            return None
        if len(self._rows) > 1:
            raise AssertionError(f"expected one row, got {len(self._rows)}")
        return self._rows[0]


def _param(params, part):
    vals = [value for key, value in params.items() if part in key]
    return vals[0] if vals else None


class _MemoryDB:
    def __init__(self, user, agents):
        self.user = user
        self.agents = {agent.id: agent for agent in agents}
        self.blocks = []
        self.passages = []
        self.added = []
        self.sql = []

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, MemoryBlock):
            self.blocks.append(obj)
        if isinstance(obj, MemoryPassage):
            if getattr(obj, "id", None) is None:
                obj.id = len(self.passages) + 1
            if not getattr(obj, "scope", None):
                obj.scope = "agent"
            if not getattr(obj, "status", None):
                obj.status = "active"
            self.passages.append(obj)

    async def delete(self, obj):
        if isinstance(obj, MemoryBlock):
            self.blocks = [row for row in self.blocks if row is not obj]
        if isinstance(obj, MemoryPassage):
            self.passages = [row for row in self.passages if row is not obj]

    async def commit(self):
        return None

    async def get(self, model, ident):
        if model is User:
            return self.user if self.user.id == ident else None
        if model is WorkspaceAgent:
            return self.agents.get(ident)
        if model is MemoryPassage:
            return next((row for row in self.passages if row.id == ident), None)
        return None

    async def execute(self, stmt, params=None):
        if params is not None:
            self.sql.append(str(stmt))
            uid, aid = params.get("uid"), params.get("aid")
            rows = [
                (row.id,)
                for row in self.passages
                if row.user_id == uid
                and getattr(row, "status", "active") == "active"
                and row.embedding is not None
                and (row.agent_id == aid or getattr(row, "scope", "agent") == "user_shared")
            ]
            return _Result(rows)
        compiled = stmt.compile().params
        entity = stmt.column_descriptions[0].get("entity")
        uid = _param(compiled, "user_id")
        aid = _param(compiled, "agent_id")
        label = _param(compiled, "label")
        if entity is MemoryBlock:
            rows = [
                row
                for row in self.blocks
                if row.user_id == uid and row.agent_id == aid and (label is None or row.label == label)
            ]
            return _Result(rows)
        if entity is MemoryPassage:
            rows = []
            for row in self.passages:
                if row.user_id != uid:
                    continue
                if getattr(row, "status", "active") in {"deleted", "superseded"}:
                    continue
                if aid is None or row.agent_id == aid or getattr(row, "scope", "agent") == "user_shared":
                    rows.append(row)
            return _Result(rows)
        return _Result([])


@pytest.fixture(autouse=True)
def _quiet_letta(monkeypatch):
    memory._letta_skip_until = 0.0

    async def _no_embed(*_args, **_kwargs):
        raise RuntimeError("no embed in unit tests")

    monkeypatch.setattr(memory, "embed_one", _no_embed)
    monkeypatch.setattr(
        memory,
        "get_settings",
        lambda: SimpleNamespace(letta_base_url="", letta_api_key="", letta_agent_model="", openai_model="x"),
    )
    yield
    memory._letta_skip_until = 0.0


def _user():
    return SimpleNamespace(
        id=uuid.uuid4(),
        display_name="Ada",
        email="ada@example.com",
        letta_agent_id="letta-chief",
        timezone="Asia/Kolkata",
    )


def _agent(user, name, kind="custom", letta_id=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user.id,
        name=name,
        title=name,
        instructions=f"{name} instructions",
        kind=kind,
        letta_agent_id=letta_id,
        color="#2f6bff",
    )


@pytest.mark.asyncio
async def test_fallback_memory_is_isolated_between_agents():
    user = _user()
    user.letta_agent_id = None
    home = _agent(user, "Home")
    dev = _agent(user, "Dev")
    db = _MemoryDB(user, [home, dev])
    db.blocks.extend(
        [
            MemoryBlock(user_id=user.id, agent_id=home.id, label="human", value="landlord is Ada"),
            MemoryBlock(user_id=user.id, agent_id=dev.id, label="human", value="repo is checkme"),
        ]
    )
    await memory.remember(db, user.id, "episodic", "home pays rent on the 1st", home.id)
    await memory.remember(db, user.id, "episodic", "dev uses pytest", dev.id)
    await memory.upsert_core(db, user.id, "voice", "warm and short", home.id)
    await memory.upsert_core(db, user.id, "voice", "terse diffs", dev.id)

    home_mem = await memory.load_memory(db, user, "rent", home)
    dev_mem = await memory.load_memory(db, user, "pytest", dev)

    assert "Ada" in home_mem and "warm and short" in home_mem and "rent" in home_mem
    assert "checkme" not in home_mem and "pytest" not in home_mem and "terse" not in home_mem
    assert "checkme" in dev_mem and "terse diffs" in dev_mem and "pytest" in dev_mem
    assert "Ada" not in dev_mem and "rent" not in dev_mem and "warm" not in dev_mem
    assert {row.agent_id for row in db.passages} == {home.id, dev.id}


@pytest.mark.asyncio
async def test_shared_facts_are_visible_to_every_agent_of_that_user(monkeypatch):
    user = _user()
    other = _user()
    chief = _agent(user, "Chief", kind="default")
    home = _agent(user, "Home")
    dev = _agent(user, "Dev")
    stranger = _agent(other, "Home")
    db = _MemoryDB(user, [chief, home, dev, stranger])

    async def fake_default(_db, _user):
        return chief

    monkeypatch.setattr("app.agents.service.ensure_default_agent", fake_default)
    await memory.remember(db, user.id, "archival", "I live in Bengaluru", home.id, scope=memory.SCOPE_SHARED)
    home_mem = await memory.load_memory(db, user, "Bengaluru", home)
    dev_mem = await memory.load_memory(db, user, "city", dev)
    other_db = _MemoryDB(other, [stranger])
    other_mem = await memory.load_memory(other_db, other, "Bengaluru", stranger)
    assert "Bengaluru" in home_mem and "Bengaluru" in dev_mem
    assert "Bengaluru" not in other_mem
    assert all(row.scope == memory.SCOPE_SHARED for row in db.passages if "Bengaluru" in row.text)


@pytest.mark.asyncio
async def test_forgotten_passage_is_not_retrieved():
    user = _user()
    home = _agent(user, "Home")
    db = _MemoryDB(user, [home])
    await memory.remember(db, user.id, "archival", "favorite color is blue", home.id)
    ctx = RunContext(db=db, user=user, connectors={}, agent=home)
    out = await memory.forget_memory(ctx, text="favorite color is blue")
    assert out.get("ok") is True
    home_mem = await memory.load_memory(db, user, "color", home)
    assert "blue" not in home_mem
    assert db.passages[0].status == memory.STATUS_DELETED


@pytest.mark.asyncio
async def test_historical_intent_injects_dated_snippet():
    user = _user()
    home = _agent(user, "Home")
    db = _MemoryDB(user, [home])
    assert memory.historical_intent("what did we decide last month") is True
    assert memory.historical_intent("what's the weather") is False
    assembled = memory._assemble_memory(
        [MemoryBlock(user_id=user.id, agent_id=home.id, label="voice", value="short answers")],
        [],
        ["2026-09-01: we chose Postgres for Checkme"],
    )
    assert "Persona" in assembled and "short answers" in assembled
    assert "2026-09-01" in assembled and "Postgres" in assembled


@pytest.mark.asyncio
async def test_load_memory_injects_history_on_past_questions(monkeypatch):
    user = _user()
    home = _agent(user, "Home")
    db = _MemoryDB(user, [home])

    async def fake_hist(*_args, **_kwargs):
        return ["2026-09-01: we chose Postgres for Checkme"]

    monkeypatch.setattr(memory, "_search_agent_history", fake_hist)
    text = await memory.load_memory(db, user, "what did we decide last month", home)
    assert "Postgres" in text and "From earlier chats" in text


@pytest.mark.asyncio
async def test_chat_learning_is_agent_scoped_and_rejects_credentials():
    user = _user()
    home = _agent(user, "Home")
    dev = _agent(user, "Dev")
    db = _MemoryDB(user, [home, dev])

    saved = await memory.learn_for_agent(db, user, home, "routine", "Remind me about rent on the first.")
    denied = await memory.learn_for_agent(db, user, home, "private", "Remember my password: abc123")
    home_mem = await memory.load_memory(db, user, "rent", home)
    dev_mem = await memory.load_memory(db, user, "rent", dev)

    assert saved["saved"] is True and denied["saved"] is False
    assert "Remind me about rent" in home_mem
    assert "Remind me about rent" not in dev_mem


@pytest.mark.asyncio
async def test_letta_memory_reads_and_writes_only_that_agent(monkeypatch):
    user = _user()
    home = _agent(user, "Home", letta_id="letta-home")
    dev = _agent(user, "Dev", letta_id="letta-dev")
    posts = []

    class _Client:
        async def get(self, url, headers=None, timeout=None, params=None):
            if "archival" in url:
                return _Resp({"passages": []})
            if "letta-home" in url:
                return _Resp({"memory": {"blocks": [{"label": "human", "value": "rent is 2000"}]}})
            if "letta-dev" in url:
                return _Resp({"memory": {"blocks": [{"label": "human", "value": "repo is checkme"}]}})
            return _Resp({}, 404)

        async def post(self, url, json=None, headers=None, timeout=None):
            posts.append((url, json))
            return _Resp({"id": "ok"})

        async def patch(self, url, json=None, headers=None, timeout=None):
            posts.append((url, json))
            return _Resp({})

    monkeypatch.setattr(
        memory,
        "get_settings",
        lambda: SimpleNamespace(letta_base_url="http://letta:8283", letta_api_key="", letta_agent_model="", openai_model="x"),
    )
    monkeypatch.setattr(memory, "shared_client", lambda: _Client())
    db = _MemoryDB(user, [home, dev])
    db.passages.append(
        MemoryPassage(user_id=user.id, agent_id=dev.id, kind="episodic", text="dev secret token", embedding=None)
    )
    db.passages.append(
        MemoryPassage(user_id=user.id, agent_id=home.id, kind="episodic", text="home backup note", embedding=None)
    )

    home_mem = await memory.load_memory(db, user, "rent", home)
    dev_mem = await memory.load_memory(db, user, "repo", dev)
    assert "rent is 2000" in home_mem and "home backup note" in home_mem
    assert "checkme" not in home_mem and "dev secret" not in home_mem
    assert "repo is checkme" in dev_mem and "dev secret token" in dev_mem
    assert "rent is 2000" not in dev_mem and "home backup" not in dev_mem

    await memory.remember(db, user.id, "episodic", "home: call the landlord", home.id)
    urls = [url for url, _body in posts]
    assert any("letta-home" in url and "archival-memory" in url for url in urls)
    assert not any("letta-dev" in url for url in urls)


@pytest.mark.asyncio
async def test_custom_agent_does_not_reuse_chief_letta_id(monkeypatch):
    user = _user()
    posts = []

    class _Client:
        async def post(self, url, json=None, headers=None, timeout=None):
            posts.append(json)
            return _Resp({"id": "letta-dev"})

    monkeypatch.setattr(
        memory,
        "get_settings",
        lambda: SimpleNamespace(letta_base_url="http://letta:8283", letta_api_key="", letta_agent_model="", openai_model="x"),
    )
    monkeypatch.setattr(memory, "shared_client", lambda: _Client())
    dev = _agent(user, "Dev")
    created = await memory.ensure_letta_agent(user, dev)
    assert created == "letta-dev"
    assert posts[0]["name"] == f"ws-{dev.id}"
    assert posts[0]["name"] != f"user-{user.id}"
    chief = _agent(user, "Chief", kind="default")
    assert await memory.ensure_letta_agent(user, chief) == "letta-chief"
    assert len(posts) == 1


@pytest.mark.asyncio
async def test_create_from_prompt_stores_agent_id(monkeypatch):
    user = _user()
    chief = _agent(user, "Chief", kind="default")
    research = _agent(user, "Research")

    async def fake_get(_db, _user, agent_id):
        assert agent_id == research.id
        return research

    async def fake_default(_db, _user):
        return chief

    async def parsed(_text, _tz):
        return {
            "timed": True,
            "title": "Scan",
            "instruction": "Scan papers",
            "schedule_kind": "weekly",
            "schedule_label": "weekly",
            "next_run_at": datetime.now(timezone.utc),
        }

    async def no_apps(*_args, **_kwargs):
        return []

    monkeypatch.setattr("app.tasks.service.get_agent", fake_get)
    monkeypatch.setattr("app.tasks.service.ensure_default_agent", fake_default)
    monkeypatch.setattr("app.tasks.service.parse_schedule", parsed)
    monkeypatch.setattr("app.tasks.service.infer_apps", no_apps)

    class _DB:
        def __init__(self):
            self.added = []

        def add(self, obj):
            self.added.append(obj)

        async def commit(self):
            return None

        async def refresh(self, _obj):
            return None

    db = _DB()
    created = await create_from_prompt(db, user, "every week scan papers", agent_id=research.id)
    assert db.added[0].agent_id == research.id
    assert created["agent_id"] == str(research.id)
    assert created["agent_name"] == "Research"
    chief_task = await create_from_prompt(db, user, "every week scan papers")
    assert db.added[1].agent_id == chief.id
    assert chief_task["agent_name"] == "Chief"


@pytest.mark.asyncio
async def test_execute_task_runs_as_owning_agent(monkeypatch):
    user = _user()
    home = _agent(user, "Home")
    dev = _agent(user, "Dev")
    captured = []
    home_chat = ChatSession(id=uuid.uuid4(), user_id=user.id, title="Tony chat", state={}, agent_id=home.id)

    async def fake_run(*_args, **kwargs):
        captured.append(kwargs)
        return {"text": "done", "used_tools": []}

    async def no_apps(*_args, **_kwargs):
        return []

    monkeypatch.setattr("app.tasks.service.run_deep_agent", fake_run)
    monkeypatch.setattr("app.tasks.service.apps_from_used", no_apps)

    class _DB:
        def __init__(self):
            self.added = []
            self.agents = {home.id: home, dev.id: dev}

        def add(self, obj):
            self.added.append(obj)

        async def commit(self):
            return None

        async def flush(self):
            return None

        async def get(self, model, ident):
            if model is WorkspaceAgent:
                return self.agents.get(ident)
            if model is ChatSession and ident == home_chat.id:
                return home_chat
            return None

    db = _DB()
    for owner in (home, dev):
        task = ScheduledTask(
            user_id=user.id,
            agent_id=owner.id,
            title=owner.name,
            instruction="Do the job",
            prompt="do the job",
            schedule_kind="once",
            schedule_label="once",
            next_run_at=datetime.now(timezone.utc),
            status="active",
            connected_apps=[],
            run_count=0,
            chat_session_id=home_chat.id if owner is home else None,
        )
        await execute_task(db, user, task, from_scheduler=False)
    assert [item["workspace_agent"].name for item in captured] == ["Home", "Dev"]
    assert all(item["auto_confirm"] is True for item in captured)
    sessions = [obj for obj in db.added if isinstance(obj, ChatSession)]
    assert [session.agent_id for session in sessions] == [dev.id]
    messages = [obj for obj in db.added if isinstance(obj, ChatMessage)]
    assert messages and {message.session_id for message in messages if message.user_id == user.id} >= {home_chat.id}


@pytest.mark.asyncio
async def test_schedule_tool_uses_speaking_agent(monkeypatch):
    seen = {}

    async def fake_create(_db, _user, prompt, agent_id=None, **_kwargs):
        seen["agent_id"] = agent_id
        seen["prompt"] = prompt
        return {"id": "t", "agent_id": str(agent_id)}

    monkeypatch.setattr("app.tasks.service.create_from_prompt", fake_create)
    user = _user()
    agent = _agent(user, "Research")
    ctx = RunContext(db=None, user=user, connectors={}, agent=agent)
    out = await run_staff(ctx, "schedule_task", {"prompt": "tomorrow email the brief"})
    assert seen["agent_id"] == agent.id
    assert seen["prompt"] == "tomorrow email the brief"
    assert out["created"] is True
    staff_out = await dispatch(None, user, None, "schedule_task", {"prompt": "in 5 minutes ping"}, agent=agent)
    assert staff_out["created"] is True


def test_backfill_assigns_each_users_chief_and_keeps_existing_owners():
    agents = [
        {"id": "c1", "user_id": "u1", "name": "Chief", "kind": "default"},
        {"id": "h1", "user_id": "u1", "name": "Home", "kind": "custom"},
        {"id": "c2", "user_id": "u2", "name": "Chief", "kind": "default"},
    ]
    owners = owner_by_user(agents)
    rows = assign_missing(
        [
            {"user_id": "u1", "label": "voice", "agent_id": None},
            {"user_id": "u2", "label": "voice", "agent_id": None},
            {"user_id": "u1", "label": "note", "agent_id": "h1"},
        ],
        owners,
    )
    assert [row["agent_id"] for row in rows] == ["c1", "c2", "h1"]
    statements = backfill_statements(owners)
    tables = {sql.split()[1] for sql, _params in statements}
    assert tables == set(SCOPED_TABLES)
    sql, params = next(item for item in statements if item[0].startswith("UPDATE memory_blocks") and item[1]["uid"] == "u1")
    assert params["aid"] == "c1"
    assert "agent_id IS NULL" in sql
    other = next(item for item in statements if item[0].startswith("UPDATE memory_passages") and item[1]["uid"] == "u2")
    assert other[1]["aid"] == "c2"
    assert choose_default_agent(
        [
            {"id": "old", "user_id": "u", "name": "Chief", "kind": "custom"},
            {"id": "real", "user_id": "u", "name": "Primary", "kind": "default"},
        ]
    )["id"] == "real"
    assert choose_default_agent([{"id": "old", "user_id": "u", "name": "Chief", "kind": "custom"}])["id"] == "old"
    assert "kind = 'default'" in PROMOTE_NAMED_CHIEF_SQL
    assert "Chief" in INSERT_MISSING_CHIEF_SQL
    assert "letta_agent_id" in COPY_CHIEF_LETTA_SQL
