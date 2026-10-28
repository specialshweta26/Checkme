"""Live, task-grounded status lines for chat turns (app.agent.progress)."""

import json
import uuid

import pytest
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import StructuredTool

from app.agent import progress as P


class FakeRedis:
    def __init__(self):
        self.data: dict[str, list] = {}
        self.ttl: dict[str, int] = {}

    async def rpush(self, key, value):
        self.data.setdefault(key, []).append(value)

    async def expire(self, key, seconds):
        self.ttl[key] = seconds

    async def lrange(self, key, start, end):
        return list(self.data.get(key, []))

    async def set(self, key, value, ex=None):
        self.data[key] = value
        if ex:
            self.ttl[key] = ex

    async def get(self, key):
        value = self.data.get(key)
        return value.encode() if isinstance(value, str) else value


class BrokenRedis:
    async def rpush(self, *a):
        raise ConnectionError("down")

    async def expire(self, *a):
        raise ConnectionError("down")

    async def lrange(self, *a):
        raise ConnectionError("down")


NAMES = {"gmail": "Gmail", "github": "GitHub", "computer": "Computer"}


def test_progress_id_validation():
    assert P.valid_progress_id("abcDEF12_-")
    assert not P.valid_progress_id("short")
    assert not P.valid_progress_id("../../etc/passwd")
    assert not P.valid_progress_id(None)


def test_humanize_tool_names():
    assert P.humanize_tool("GMAIL_SEARCH_MESSAGES", "gmail") == "search messages"
    assert P.humanize_tool("list_events") == "events"
    assert P.humanize_tool("gateway__tool_search") == "search"
    assert P.humanize_tool("createIssue") == "create issue"


@pytest.mark.parametrize(
    "name,args,expected",
    [
        ("list_connectors", {}, "Checking which apps you have connected"),
        ("search_tools", {"query": "find unread invoices"}, "Looking for a tool that can find unread invoices"),
        ("search_tools", {"query": "open pr", "connector_slug": "github"}, "Looking for a tool in GitHub that can open pr"),
        (
            "call_tool",
            {"connector_slug": "gmail", "tool_name": "GMAIL_SEARCH_MESSAGES", "args": {"query": "invoice"}},
            "Gmail: search messages for “invoice”",
        ),
        (
            "call_tool",
            {"connector_slug": "computer", "tool_name": "browser_navigate", "args": {"url": "https://www.example.com/a"}},
            "Opening example.com in the browser",
        ),
        (
            "call_tool",
            {"connector_slug": "gmail", "tool_name": "gateway__tool_search", "args": {"query": "send mail"}},
            "Finding the right Gmail tool for “send mail”",
        ),
        ("create_agent_group", {"name": "Crew", "all_agents": True}, "Creating the group “Crew” with all your agents"),
        ("create_agent_group", {"name": "Duo", "members": ["Test", "Ori"]}, "Creating the group “Duo” with Test, Ori"),
        ("delete_agent_group", {"target": "Crew"}, "Preparing a delete confirmation for “Crew”"),
        ("run_staff_tool", {"name": "today_briefing"}, "Putting together today's briefing"),
        ("schedule_task", {"prompt": "email me the report at 9am"}, "Scheduling: email me the report at 9am"),
        (
            "write_todos",
            {"todos": [{"content": "Find the PR", "status": "completed"}, {"content": "Review the diff", "status": "in_progress"}]},
            "Next: Review the diff",
        ),
        ("read_file", {"file_path": "/notes.md"}, "Reading /notes.md"),
        ("inspect_result", {"search": "total"}, "Reading the results for “total”"),
        ("web_search", {"query": "Pune weather"}, "Searching the web for “Pune weather”"),
        ("browser_navigate", {"url": "https://www.example.com/a"}, "Opening example.com in the browser"),
        ("remember", {"text": "prefers IST"}, "Saving that to memory"),
        ("scratchpad_add", {"text": "Open the PR"}, "Adding to the checklist: Open the PR"),
        ("task_catalog", {"action": "search", "query": "daily briefing"}, "Looking for a task that can daily briefing"),
    ],
)
def test_describe_tool_is_grounded_in_the_call(name, args, expected):
    assert P.describe_tool(name, args, NAMES) == expected


def test_describe_unknown_tool_falls_back_to_its_name_and_key_arg():
    assert P.describe_tool("fetch_weather_report", {"query": "Pune"}, {}) == "Weather report: Pune"


def test_unknown_connector_slug_is_titled():
    line = P.describe_tool("call_tool", {"connector_slug": "google-drive", "tool_name": "find_file", "args": {"name": "Q3"}}, {})
    assert line == "Google Drive: find file: Q3"


async def test_reporter_writes_to_redis_and_dedupes():
    r = FakeRedis()
    uid = uuid.uuid4()
    rep = P.ProgressReporter(uid, "turn_12345678", redis_client=r)
    await rep.emit("Searching Gmail")
    await rep.emit("Searching Gmail")
    await rep.emit("Writing the reply", kind="reply")
    await rep.finish()
    steps = await P.read_progress(uid, "turn_12345678", redis_client=r)
    assert [s["text"] for s in steps] == ["Searching Gmail", "Writing the reply", "Done"]
    assert steps[-1]["kind"] == "done"
    assert r.ttl[f"chatprog:{uid}:turn_12345678"] == P.PROGRESS_TTL_S


async def test_reporter_without_id_keeps_steps_locally_only():
    r = FakeRedis()
    rep = P.ProgressReporter("u", None, redis_client=r)
    await rep.emit("Hello")
    assert rep.steps[0]["text"] == "Hello" and r.data == {}


async def test_reporter_survives_redis_outage():
    rep = P.ProgressReporter("u", "turn_12345678", redis_client=BrokenRedis())
    await rep.emit("Still fine")
    assert await P.read_progress("u", "turn_12345678", redis_client=BrokenRedis()) == []


async def test_child_reporter_prefixes_teammate_name():
    r = FakeRedis()
    rep = P.ProgressReporter("u", "turn_12345678", redis_client=r)
    await rep.child("Dev").emit("GitHub: create pull request")
    steps = await P.read_progress("u", "turn_12345678", redis_client=r)
    assert steps[0]["text"] == "Dev: GitHub: create pull request"


async def test_read_progress_rejects_bad_ids():
    assert await P.read_progress("u", "bad id!", redis_client=FakeRedis()) == []


async def test_callback_narrates_real_model_and_tool_events():
    r = FakeRedis()
    rep = P.ProgressReporter("u", "turn_12345678", redis_client=r)
    cb = P.ProgressCallback(rep, query="find my invoice from Acme", connector_names=NAMES)

    async def call_tool(connector_slug: str, tool_name: str, args: dict | None = None) -> str:
        return json.dumps({"ok": True})

    from app.agent.deep import CallToolInput

    tool = StructuredTool.from_function(coroutine=call_tool, name="call_tool", description="run", args_schema=CallToolInput)
    model = GenericFakeChatModel(
        messages=iter(
            [
                AIMessage(content="I'll search your Gmail for Acme invoices. Then summarise.", tool_calls=[{"name": "call_tool", "args": {}, "id": "1"}]),
                AIMessage(content="Here is the invoice."),
            ]
        )
    )
    await model.ainvoke("hi", config={"callbacks": [cb]})
    await tool.ainvoke(
        {"connector_slug": "gmail", "tool_name": "GMAIL_SEARCH_MESSAGES", "args": {"query": "Acme invoice"}},
        config={"callbacks": [cb]},
    )
    await model.ainvoke("hi", config={"callbacks": [cb]})
    texts = [s["text"] for s in rep.steps]
    assert texts == [
        "Working out how to handle “find my invoice from Acme”",
        "I'll search your Gmail for Acme invoices.",
        "Gmail: search messages for “Acme invoice”",
        "Going over what Gmail returned",
        "Writing the reply",
    ]
    assert not any(t in {"Thinking", "Checking connected sources"} for t in texts)


async def test_callback_names_the_teammate():
    rep = P.ProgressReporter("u", None)
    cb = P.ProgressCallback(rep, agent_names={"a1": "Dev"})
    tool = StructuredTool.from_function(
        coroutine=lambda agent_id, prompt: _ok(), name="ask_teammate", description="ask"
    )
    await tool.ainvoke({"agent_id": "a1", "prompt": "open a PR for the fix"}, config={"callbacks": [cb]})
    assert rep.steps[-1]["text"] == "Asking Dev: open a PR for the fix"


async def _ok():
    return "ok"


async def test_explicit_callbacks_replace_inherited_ones_in_nested_runs():
    """Teammate runs pass their own (prefixed) callbacks; the parent's must not double-report."""

    class Recorder(AsyncCallbackHandler):
        def __init__(self):
            self.names = []

        async def on_tool_start(self, serialized, input_str, **kw):
            self.names.append(serialized.get("name"))

    parent, child = Recorder(), Recorder()
    inner = StructuredTool.from_function(coroutine=lambda x: _ok(), name="inner", description="i")

    async def outer_fn(x: str) -> str:
        return await inner.ainvoke({"x": x}, config={"callbacks": [child]})

    outer = StructuredTool.from_function(coroutine=outer_fn, name="outer", description="o")
    await RunnableLambda(lambda v: v).ainvoke(1)
    await outer.ainvoke({"x": "1"}, config={"callbacks": [parent]})
    assert parent.names == ["outer"]
    assert child.names == ["inner"]


async def test_run_deep_agent_streams_live_steps(monkeypatch):
    """The real agent loop attaches ProgressCallback, so tool events become status lines."""
    from types import SimpleNamespace

    import app.connectors.platform as platform
    from app.agent import deep

    async def noop(*a, **k):
        return None

    async def conns(*a, **k):
        return [SimpleNamespace(slug="github", name="GitHub", id=1)]

    async def no_assign(*a, **k):
        return {}

    async def memory(*a, **k):
        return ""

    class Graph:
        async def ainvoke(self, payload, config=None):
            cbs = (config or {}).get("callbacks") or []
            assert cbs and isinstance(cbs[0], P.ProgressCallback)
            tool = StructuredTool.from_function(
                coroutine=lambda connector_slug, tool_name, args=None: _ok(),
                name="call_tool",
                description="run",
                args_schema=deep.CallToolInput,
            )
            await tool.ainvoke(
                {"connector_slug": "github", "tool_name": "create_pull_request", "args": {"title": "Fix status"}},
                config={"callbacks": cbs},
            )
            return {"messages": [AIMessage(content="Opened it.")]}

    monkeypatch.setattr(platform, "ensure_platform_connectors", noop)
    monkeypatch.setattr(deep, "user_connectors", conns)
    monkeypatch.setattr(deep, "connector_assignment_map", no_assign)
    monkeypatch.setattr(deep, "toolkit_assignment_map", no_assign)
    monkeypatch.setattr(deep, "filter_connectors", lambda c, *a: c)
    monkeypatch.setattr(deep, "load_memory", memory)
    monkeypatch.setattr(deep, "_maybe_start_connect", noop)
    monkeypatch.setattr(deep, "build_agent", lambda ctx, prompt: Graph())
    monkeypatch.setattr(deep, "system_prompt", lambda *a, **k: "")

    r = FakeRedis()
    rep = P.ProgressReporter("u1", "turn_12345678", redis_client=r)
    out = await deep.run_deep_agent(
        None, SimpleNamespace(id="u1", timezone="Asia/Kolkata"), uuid.uuid4(), "r1", [], "open a PR", progress=rep
    )
    assert out["text"] == "Opened it."
    texts = [s["text"] for s in await P.read_progress("u1", "turn_12345678", redis_client=r)]
    assert texts == ["Recalling what I know about this", "GitHub: create pull request: Fix status"]


def test_progress_route_returns_steps_and_done(monkeypatch):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.chat import routes
    from app.deps import current_user

    r = FakeRedis()
    uid = uuid.uuid4()

    async def fake_read(user_id, progress_id):
        return await P.read_progress(user_id, progress_id, redis_client=r)

    async def fake_session(user_id, progress_id):
        return await P.read_progress_session(user_id, progress_id, redis_client=r)

    monkeypatch.setattr(routes, "read_progress", fake_read)
    monkeypatch.setattr(routes, "read_progress_session", fake_session)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=uid)
    client = TestClient(app)

    import asyncio

    rep = P.ProgressReporter(uid, "turn_12345678", redis_client=r)
    asyncio.run(rep.emit("Gmail: search messages for “invoice”"))
    body = client.get("/chat/progress/turn_12345678").json()
    assert body == {"steps": [rep.steps[0]], "done": False, "session_id": None}
    asyncio.run(rep.set_session("sess-1"))
    assert client.get("/chat/progress/turn_12345678").json()["session_id"] == "sess-1"
    asyncio.run(rep.finish())
    assert client.get("/chat/progress/turn_12345678").json()["done"] is True
    assert client.get("/chat/progress/bad").status_code == 400


async def test_set_session_round_trips_and_ignores_missing_ids():
    r = FakeRedis()
    rep = P.ProgressReporter("u", "turn_12345678", redis_client=r)
    await rep.set_session("abc")
    assert await P.read_progress_session("u", "turn_12345678", redis_client=r) == "abc"
    assert r.ttl["chatprog:u:turn_12345678:session"] == P.PROGRESS_TTL_S
    await P.ProgressReporter("u", None, redis_client=r).set_session("zzz")
    assert await P.read_progress_session("u", "bad id!", redis_client=r) == ""
