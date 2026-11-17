"""Less context per model call, and less fixed work per turn (docs/LLD.md "Lean context")."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent import deep, finish_check, tool_scope
from app.agent import fast_path


def _tools():
    user = SimpleNamespace(id="u1", timezone="Asia/Kolkata", display_name="G", email="g@x.test")
    agent = SimpleNamespace(id="a1", name="Chief", title="chief of staff", instructions="", focus="", kind="chief")
    return deep.harness_tools(deep.RunContext(db=None, user=user, connectors={}, agent=agent))


def _chars(tools) -> int:
    from langchain_core.utils.function_calling import convert_to_openai_tool

    return sum(len(json.dumps(convert_to_openai_tool(t))) for t in tools)


# --- tool schemas ------------------------------------------------------------------------------------------------


def test_a_data_question_is_sent_far_fewer_tool_schemas():
    tools = _tools()
    hidden = tool_scope.hidden_groups("One8 sales in last 19 days", [], fast_path.DIRECT)
    assert {"browser", "computer", "agents", "media"} <= set(hidden)
    kept = tool_scope.scope_tools(tools, set(hidden))
    names = {t.name for t in kept}
    assert {"call_tool", "search_tools", "web_search", "inspect_result", "read_history", "recall_memory"} <= names
    assert not any(n.startswith("browser_") for n in names)
    assert _chars(kept) < _chars(tools) * 0.5


@pytest.mark.parametrize(
    "query,kind,shown",
    [
        ("book a flight from Delhi to Goa on 3 Dec", fast_path.COMPUTER, "browser"),
        ("check https://example.test/pricing for me", fast_path.DIRECT, "browser"),
        ("make a deck on Q3 sales", fast_path.DIRECT, "office"),
        ("create an agent for my sales follow ups", fast_path.DIRECT, "agents"),
        ("draw a logo for my bakery", fast_path.DIRECT, "media"),
        ("open Notes on my mac", fast_path.COMPUTER, "computer"),
        ("save this as a skill", fast_path.DIRECT, "skill_authoring"),
    ],
)
def test_a_message_that_points_at_a_group_keeps_it(query, kind, shown):
    assert shown not in tool_scope.hidden_groups(query, [], kind)


def test_a_follow_up_keeps_the_tools_the_last_reply_used():
    history = [{"role": "user", "content": "find hotels in Goa"},
               {"role": "assistant", "content": "Here are three.\n\nTools: browser_navigate ok, browser_collect ok"}]  # fmt: skip
    assert "browser" not in tool_scope.hidden_groups("the second one", history, fast_path.DIRECT)


def test_jobs_teammates_and_skills_keep_every_tool(monkeypatch):
    assert tool_scope.hidden_groups("One8 sales", [], fast_path.DIRECT, full=True) == []
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "lean_context", False)
    assert tool_scope.hidden_groups("One8 sales", [], fast_path.DIRECT) == []


def test_a_group_comes_back_once_loaded_or_used():
    hidden = ["browser", "media"]
    loaded = [AIMessage(content="", tool_calls=[{"id": "1", "name": "use_tools", "args": {"group": "browser"}}])]
    assert tool_scope.visible_hidden(hidden, loaded) == {"media"}
    used = [AIMessage(content="", tool_calls=[{"id": "2", "name": "generate_image", "args": {}}])]
    assert tool_scope.visible_hidden(hidden, used) == {"browser"}


async def test_the_middleware_sends_only_the_turns_tools():
    tools = _tools()
    seen = {}

    class Request:
        def __init__(self, messages, tools):
            self.messages, self.tools = messages, tools

        def override(self, **kw):
            return Request(kw.get("messages", self.messages), kw.get("tools", self.tools))

    async def handler(request):
        seen["names"] = {t.name for t in request.tools}
        return "ok"

    mw = tool_scope.middleware(["browser"])
    await mw.awrap_model_call(Request([HumanMessage(content="q")], tools), handler)
    assert not any(n.startswith("browser_") for n in seen["names"]) and "web_search" in seen["names"]
    loaded = [HumanMessage(content="q"), AIMessage(content="", tool_calls=[{"id": "1", "name": "use_tools", "args": {"group": "browser"}}]),
              ToolMessage(content="{}", tool_call_id="1", name="use_tools")]  # fmt: skip
    await mw.awrap_model_call(Request(loaded, tools), handler)
    assert "browser_navigate" in seen["names"]


async def test_a_hidden_tool_the_model_calls_anyway_still_runs():
    """Only the schemas sent to the model change: the agent can still run every tool."""
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.tools import StructuredTool

    ran = []

    async def browser_navigate(url: str) -> str:
        """Open a page."""
        ran.append(url)
        return "opened"

    async def web_search(query: str) -> str:
        """Search."""
        return "results"

    class Model(GenericFakeChatModel):
        def bind_tools(self, tools, **kw):  # noqa: ANN001
            seen.append(sorted(getattr(t, "name", "") for t in tools))
            return self

    seen: list = []
    model = Model(messages=iter([
        AIMessage(content="", tool_calls=[{"id": "c1", "name": "browser_navigate", "args": {"url": "https://a.test"}}]),
        AIMessage(content="done"),
    ]))  # fmt: skip
    tools = [StructuredTool.from_function(coroutine=browser_navigate), StructuredTool.from_function(coroutine=web_search),
             tool_scope.load_tool(["browser"])]  # fmt: skip
    graph = create_agent(model=model, tools=tools, middleware=[tool_scope.middleware(["browser"])])
    out = await graph.ainvoke({"messages": [HumanMessage(content="hi")]})
    assert ran == ["https://a.test"] and out["messages"][-1].content == "done"
    assert "browser_navigate" not in seen[0] and "browser_navigate" in seen[1]


def test_the_turn_context_lists_what_is_left_out():
    note = tool_scope.context_note(["browser", "media"])
    assert "use_tools" in note and "- browser:" in note and "- media:" in note
    assert tool_scope.context_note([]) == ""


# --- history ----------------------------------------------------------------------------------------------------


def test_old_long_replies_are_shortened_and_the_last_exchange_kept_whole():
    long = "r" * 9000
    history = [{"role": "user", "content": "q1", "id": 1}, {"role": "assistant", "content": long, "id": 2},
               {"role": "user", "content": "q2", "id": 3}, {"role": "assistant", "content": long, "id": 4}]  # fmt: skip
    out = deep.shorten_old(history)
    assert len(out[1]["content"]) < 2000 and "read_history(message_id=2)" in out[1]["content"]
    assert out[3]["content"] == long and history[1]["content"] == long  # latest whole; the stored history untouched
    msgs = deep._lc_messages(history, "q3")
    assert isinstance(msgs[1], AIMessage) and len(msgs[1].content) < 2000 and msgs[-1].content == "q3"


def test_history_is_left_whole_when_switched_off(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "lean_context", False)
    history = [{"role": "assistant", "content": "r" * 9000}, {"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
    assert deep.shorten_old(history)[0]["content"] == "r" * 9000


def test_old_tool_results_are_cut_shorter():
    from app.agent import context_trim as ct

    msgs = [HumanMessage(content="q"), AIMessage(content="", tool_calls=[{"id": "a", "name": "call_tool", "args": {}}]),
            ToolMessage(content="y" * 30000, tool_call_id="a", name="call_tool"), AIMessage(content="t"),
            HumanMessage(content="n"), AIMessage(content="o"), HumanMessage(content="g")]  # fmt: skip
    out, _ = ct.trim_messages(msgs)
    assert ct.LEAN_OLD_RESULT_CHARS <= len(out[2].content) < ct.LEAN_OLD_RESULT_CHARS + 200


# --- fixed per-turn work -------------------------------------------------------------------------------------------


def _ctx(**kw):
    base = dict(teammate_depth=0, pending=[], question=None, used_tools=["call_tool", "call_tool"], active_skills=[], task_state=None, job=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_the_finish_check_runs_only_for_multi_step_work():
    direct = fast_path.TurnPlan(fast_path.DIRECT)
    assert not deep._multi_step(_ctx(), direct)  # a data question with two calls
    assert deep._multi_step(_ctx(used_tools=["write_todos", "call_tool"]), direct)
    assert deep._multi_step(_ctx(used_tools=["browser_act", "browser_act"]), direct)
    assert deep._multi_step(_ctx(active_skills=[{"id": 1}]), direct)
    assert deep._multi_step(_ctx(), fast_path.TurnPlan(fast_path.COMPUTER))
    assert deep._multi_step(_ctx(used_tools=["call_tool"] * 6), direct)
    assert not finish_check.should_check(_ctx(), "Sales were ₹4.1L.", multi_step=False)
    assert finish_check.should_check(_ctx(), "Sales were ₹4.1L.", multi_step=True)


def test_quick_web_questions_skip_skill_routing():
    web = fast_path.classify_turn("what's the weather in Pune today")
    assert web.kind == fast_path.QUICK_WEB and deep.skip_skill_routing(web, "what's the weather in Pune today")
    assert not deep.skip_skill_routing(web, "run my usual news skill today")
    assert not deep.skip_skill_routing(fast_path.TurnPlan(fast_path.DIRECT), "send the weekly sales report")
