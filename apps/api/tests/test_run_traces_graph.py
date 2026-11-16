"""The trace callback on a real LangGraph agent with a scripted (free) model: the events arrive as the tracer expects."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

langgraph_prebuilt = pytest.importorskip("langgraph.prebuilt")

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402

from app.jobs import trace  # noqa: E402


class ScriptedModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):  # noqa: ANN001
        return self


@tool
def lookup_price(item: str, api_key: str = "") -> str:
    """Look up the price of an item."""
    return '{"ok": true, "price": "$12"}'


@tool
def open_page(url: str) -> str:
    """Open a page."""
    return '{"ok": false, "error": "page is down"}'  # how the app's tools report a failure


async def test_trace_callback_on_a_real_agent_graph(monkeypatch):
    saved: list[dict] = []

    async def save(self, step, shot):  # noqa: ANN001
        saved.append(step)

    monkeypatch.setattr(trace.Tracer, "_save", save)
    model = ScriptedModel(messages=iter([
        AIMessage(content="I'll check the price first.", tool_calls=[{"name": "lookup_price", "args": {"item": "mug", "api_key": "k-1"}, "id": "t1"}]),
        AIMessage(content="Now the product page.", tool_calls=[{"name": "open_page", "args": {"url": "https://example.com"}, "id": "t2"}]),
        AIMessage(content="The mug costs $12."),
    ]))  # fmt: skip
    graph = langgraph_prebuilt.create_react_agent(model, [lookup_price, open_page])
    tracer = trace.Tracer(SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), attempts=1), agent_name="Raaz")
    token = trace.CURRENT.set(tracer)
    try:
        await graph.ainvoke({"messages": [("user", "price of a mug?")]}, config={"callbacks": trace.callbacks("Raaz", 0, {})})
    finally:
        trace.CURRENT.reset(token)
    await tracer.flush()
    assert [s.get("kind", "tool") for s in saved] == ["tool", "tool"]  # the reply step comes from run_job
    price, page = saved
    assert price["tool"] == "lookup_price" and price["why"] == "I'll check the price first." and price["ok"]
    assert price["args"] == {"item": "mug", "api_key": trace.HIDDEN} and "$12" in price["result"]
    assert page["tool"] == "open_page" and page["why"] == "Now the product page." and not page["ok"]
    assert page["error"] == "page is down"
    assert [s["i"] for s in saved] == [1, 2]
