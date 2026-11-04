"""A run that hits the step limit continues from where it stopped, so finished tool calls don't repeat."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from deepagents import create_deep_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from app.agent import deep


class _Model(GenericFakeChatModel):
    def bind_tools(self, tools, **kw):
        return self


def test_a_real_graph_continues_without_repeating_tool_calls():
    sent: list[int] = []

    @tool
    def send_email(n: int) -> str:
        """Send an email."""
        sent.append(n)
        return f"sent {n}"

    def script():
        for i in range(6):
            yield AIMessage(content="", tool_calls=[{"name": "send_email", "args": {"n": i}, "id": f"c{i}", "type": "tool_call"}])
        yield AIMessage(content="all done")

    async def run():
        graph = create_deep_agent(model=_Model(messages=script()), tools=[send_email], system_prompt="x", checkpointer=InMemorySaver())
        thread = {"configurable": {"thread_id": "t"}}
        next_input = {"messages": [("user", "go")]}
        for _ in range(5):
            try:
                return await graph.ainvoke(next_input, config={"recursion_limit": 7, **thread}, durability="exit")
            except Exception as exc:
                assert deep._is_recursion_limit(exc)
                assert await deep._can_resume(graph, thread)
                next_input = None
        raise AssertionError("never finished")

    result = asyncio.run(run())
    assert result["messages"][-1].content == "all done"
    assert sent == [0, 1, 2, 3, 4, 5]


def test_can_resume_needs_saved_messages():
    class Graph:
        def __init__(self, values=None, error=None):
            self.values, self.error = values, error

        async def aget_state(self, config):
            if self.error:
                raise self.error
            return SimpleNamespace(values=self.values)

    assert asyncio.run(deep._can_resume(Graph({"messages": ["m"]}), {}))
    assert not asyncio.run(deep._can_resume(Graph({}), {}))
    assert not asyncio.run(deep._can_resume(Graph(None), {}))
    assert not asyncio.run(deep._can_resume(Graph(error=RuntimeError("x")), {}))


def test_build_agent_keeps_a_checkpoint_for_the_turn(monkeypatch):
    monkeypatch.setattr(deep, "_chat_model", lambda: _Model(messages=iter([AIMessage(content="ok")])))
    monkeypatch.setattr(deep, "harness_tools", lambda ctx: [])
    graph = deep.build_agent(None, "x")
    assert isinstance(graph.checkpointer, InMemorySaver)

    async def run():
        result = await graph.ainvoke({"messages": [("user", "hi")]}, config={"configurable": {"thread_id": "t"}}, durability="exit")
        state = await graph.aget_state({"configurable": {"thread_id": "t"}})
        return result, state

    result, state = asyncio.run(run())
    assert result["messages"][-1].content == "ok" and state.values["messages"]
