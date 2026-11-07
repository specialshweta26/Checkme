"""The fixed prompt stays identical across turns (cacheable) and later turns know what earlier replies' tools did."""

from __future__ import annotations

import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agent import tool_digest
from app.agent.deep import system_prompt
from app.agent.turn_context import CONTEXT_HEADER, MARK, TurnContextMiddleware, split_prompt, with_turn_context


def _prompt(memory: str, query: str = "hi") -> str:
    user = SimpleNamespace(id="u1", timezone="Asia/Kolkata", email="a@b.test")
    agent = SimpleNamespace(id="a1", name="Chief", title="chief of staff", instructions="", focus="", kind="default")
    return system_prompt(user, [], memory, agent=agent, skills=[], matched_skills=[], query=query)


# --- the fixed part is the same every turn ------------------------------------------------------------------------


def test_everything_that_changes_per_turn_is_after_the_fixed_rules():
    a, b = _prompt("- Lives in Pune", "where do I live"), _prompt("- Drives a Honda", "what car")
    fixed_a, turn_a = split_prompt(a)
    fixed_b, turn_b = split_prompt(b)
    assert fixed_a == fixed_b  # identical across turns: the provider can cache it
    assert "Current date and time" not in fixed_a and "Current date and time" in turn_a
    assert "User memory:" not in fixed_a and "Lives in Pune" in turn_a and "Drives a Honda" in turn_b
    assert len(fixed_a) > 10 * len(turn_a)


def test_a_prompt_without_a_turn_section_is_all_fixed():
    assert split_prompt("rules only") == ("rules only", "")
    fixed, turn = split_prompt("rules" + MARK + "  time: now  ")
    assert (fixed, turn) == ("rules", "time: now")


def test_the_context_goes_just_before_the_latest_user_message():
    msgs = [SystemMessage("rules"), HumanMessage("first"), AIMessage("ok"), HumanMessage("second"), AIMessage("", tool_calls=[{"name": "t", "args": {}, "id": "c1", "type": "tool_call"}]), ToolMessage("r", tool_call_id="c1")]
    out = with_turn_context(msgs, "memory: Pune")
    assert [m.type for m in out] == ["system", "human", "ai", "human", "human", "ai", "tool"]
    assert out[3].content.startswith(CONTEXT_HEADER) and "memory: Pune" in out[3].content
    assert out[4].content == "second"
    assert with_turn_context(msgs, "") == msgs


async def test_the_middleware_changes_the_request_not_the_saved_state():
    seen = {}
    state_messages = [SystemMessage("rules"), HumanMessage("hello")]

    class Request:
        def __init__(self, messages):
            self.messages = messages

        def override(self, **kw):
            return Request(kw["messages"])

    async def handler(req):
        seen["messages"] = req.messages
        return "reply"

    mw = TurnContextMiddleware("time: 10:00")
    req = Request(state_messages)
    assert await mw.awrap_model_call(req, handler) == "reply"
    assert [m.type for m in seen["messages"]] == ["system", "human", "human"]
    assert len(state_messages) == 2  # nothing was added to what gets saved


# --- the tool record -------------------------------------------------------------------------------------------------


def _turn(*calls):
    msgs = [HumanMessage("old question"), AIMessage("", tool_calls=[{"name": "call_tool", "args": {"connector_slug": "x", "tool_name": "old"}, "id": "o", "type": "tool_call"}]), ToolMessage("{}", tool_call_id="o"), AIMessage("old answer"), HumanMessage("post the report")]
    for i, (name, args, result) in enumerate(calls):
        msgs.append(AIMessage("", tool_calls=[{"name": name, "args": args, "id": f"c{i}", "type": "tool_call"}]))
        if result is not None:
            msgs.append(ToolMessage(json.dumps(result), tool_call_id=f"c{i}"))
    msgs.append(AIMessage("Done."))
    return msgs


def test_the_record_lists_this_turns_tools_with_ids_and_outcomes():
    msgs = _turn(
        ("search_tools", {"query": "post"}, [{"tool_name": "post_message"}]),
        ("call_tool", {"connector_slug": "salesops", "tool_name": "get_report", "args": {"region": "all"}}, {"region": "all", "revenue_inr": 4200000}),
        ("call_tool", {"connector_slug": "salesops", "tool_name": "post_message", "args": {"channel": "sales", "text": "hi"}}, {"status": "awaiting_user_confirmation", "preview": {}}),
        ("web_search", {"query": "acme"}, {"error": "rate limited"}),
    )
    digest = tool_digest.build(msgs)
    assert [d["tool"] for d in digest] == ["salesops.get_report", "salesops.post_message", "web_search"]  # this turn only, no catalog lookups
    assert digest[1]["status"] == "waiting for the user's approval, not run"
    assert digest[2]["status"] == "error"
    text = tool_digest.render(digest)
    assert "salesops.post_message(channel=sales, text=hi) -> waiting for the user's approval, not run" in text


def test_an_approved_action_is_recorded_as_run():
    approved = {"approved_by_user": True, "status": "ran", "result": json.dumps({"ok": True, "channel": "sales", "posted": "hi"})}
    digest = tool_digest.build(_turn(("call_tool", {"connector_slug": "salesops", "tool_name": "post_message", "args": {"channel": "sales"}}, approved)))
    assert digest[0]["status"] == "ran after the user approved it"
    assert digest[0]["facts"]["channel"] == "sales"


def test_older_replies_keep_only_tools_ids_and_outcomes():
    digest = tool_digest.build(_turn(("call_tool", {"connector_slug": "gmail", "tool_name": "send_message", "args": {"to": "p@acme.test", "subject": "SOW", "body": "x" * 500}}, {"ok": True, "message_id": "m-123", "thread_id": "t-9"})))
    full, brief = tool_digest.render(digest), tool_digest.render(digest, brief=True)
    assert "message_id=m-123" in brief and "to=p@acme.test" in brief and "xxxx" not in brief
    assert len(brief) < len(full)


def test_an_unfinished_call_is_marked():
    digest = tool_digest.build(_turn(("call_tool", {"connector_slug": "s", "tool_name": "slow", "args": {}}, None)))
    assert digest[0]["status"] == "not finished"


def test_nothing_to_record_renders_nothing():
    assert tool_digest.render([]) == "" and tool_digest.build([HumanMessage("hi"), AIMessage("hello")]) == []


async def test_a_reply_waiting_for_approval_is_not_saved_as_an_episode(monkeypatch):
    """'Prepared it, not posted' would outlive the approval and later contradict what happened."""
    import uuid as _uuid

    from app.agent import memory_extract
    from app.models import MemoryExtractJob, User, WorkspaceAgent

    user = SimpleNamespace(id=_uuid.uuid4())
    agent = SimpleNamespace(id=_uuid.uuid4(), user_id=user.id)
    episodes = []

    async def record_turn(*a, **k):
        episodes.append(a[3])

    async def nothing(*a, **k):
        return [] if "known" in str(k) else {}

    monkeypatch.setattr("app.memory_engine.episodes.record_turn", record_turn)
    monkeypatch.setattr(memory_extract, "known_facts", lambda *a, **k: _async([]))
    monkeypatch.setattr(memory_extract, "_extract_json", lambda *a, **k: _async({}))
    monkeypatch.setattr(memory_extract, "read_core", lambda *a, **k: _async(""))

    def db_with(rows):
        class Rows:
            def scalars(self):
                return SimpleNamespace(all=lambda: rows)

        class DB:
            async def get(self, model, ident):
                return user if model is User else agent if model is WorkspaceAgent else None

            async def execute(self, *a, **k):
                return Rows()

        return DB()

    def turn(pending):
        return [SimpleNamespace(role="user", content="Post 'hi' to #sales", meta={}),
                SimpleNamespace(role="assistant", content="Prepared it; not posted yet.", meta={"pending_confirms": [{"token": "t"}] if pending else []})]

    job = MemoryExtractJob(user_id=user.id, agent_id=agent.id, request_id="r1", payload={})
    assert await memory_extract.process_job(db_with(turn(pending=True)), job) == "done"
    assert episodes == []
    assert await memory_extract.process_job(db_with(turn(pending=False)), job) == "done"
    assert episodes == ["Post 'hi' to #sales"]


def _async(value):
    async def coro():
        return value

    return coro()


def test_the_prompt_knows_who_the_user_is_in_its_fixed_part():
    user = SimpleNamespace(id="u1", timezone="Asia/Kolkata", email="gaurav@example.test", display_name="Gaurav R")
    agent = SimpleNamespace(id="a1", name="Chief", title="chief of staff", instructions="", focus="", kind="default")
    fixed, _turn = split_prompt(system_prompt(user, [], "", agent=agent, skills=[], matched_skills=[], query="what is my name"))
    assert "The user you work for: Gaurav R <gaurav@example.test>" in fixed  # cached with the rest of the fixed part


def test_questions_about_the_user_are_not_web_lookups():
    from app.agent import fast_path

    for q in ("What is my name?", "what is my email", "Who am I?", "Do you know my name?", "What do you know about me?"):
        assert fast_path.classify_turn(q).kind == fast_path.DIRECT, q
    for q in ("What is the weather today?", "What is the price of gold?", "Any news update on my flight?"):
        assert fast_path.classify_turn(q).kind == fast_path.QUICK_WEB, q


async def test_a_turn_that_forgot_something_adds_nothing_back(monkeypatch):
    """"Forget everything about Priya" mentions Priya; extracting from it re-saved "Priya Nair is a person"."""
    import uuid as _uuid

    from app.agent import memory_extract
    from app.models import MemoryExtractJob, User, WorkspaceAgent

    user = SimpleNamespace(id=_uuid.uuid4())
    agent = SimpleNamespace(id=_uuid.uuid4(), user_id=user.id)
    calls = []

    async def record_turn(*a, **k):
        calls.append("episode")

    async def extract(*a, **k):
        calls.append("extract")
        return {"facts": [{"text": "Priya Nair is a person"}]}

    monkeypatch.setattr("app.memory_engine.episodes.record_turn", record_turn)
    monkeypatch.setattr(memory_extract, "_extract_json", extract)
    monkeypatch.setattr(memory_extract, "known_facts", lambda *a, **k: _async([]))

    class Rows:
        def scalars(self):
            return SimpleNamespace(all=lambda: [
                SimpleNamespace(role="user", content="Forget everything you remember about Priya Nair.", meta={}),
                SimpleNamespace(role="assistant", content="Forgot everything about Priya Nair.", meta={"used_tools": ["forget_memory"]}),
            ])

    class DB:
        async def get(self, model, ident):
            return user if model is User else agent if model is WorkspaceAgent else None

        async def execute(self, *a, **k):
            return Rows()

    job = MemoryExtractJob(user_id=user.id, agent_id=agent.id, request_id="r1", payload={})
    assert await memory_extract.process_job(DB(), job) == "done"
    assert calls == []
