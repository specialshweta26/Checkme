"""A connector's answer reaches the model whole (docs/LLD.md "Connector results"): a product-wise breakdown over the old
24,000-character cap was replaced by an outline with no figures, and the agent never saw the data."""

from __future__ import annotations

import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent import context_trim, deep

PRODUCTS = {"brand": "x", "window": {"start": "2026-11-11", "end": "2026-11-17"},
            "products": [{"sku": f"SKU-{i}", "name": f"Product {i}", "units": i + 1, "net_sales": (i + 1) * 1999} for i in range(1500)]}  # fmt: skip


def test_a_big_answer_is_sent_whole():
    assert len(json.dumps(PRODUCTS)) > 60000
    assert deep.fit_payload(PRODUCTS) == PRODUCTS


def test_an_answer_too_big_for_the_model_keeps_real_rows_not_an_outline():
    out = deep.fit_payload(PRODUCTS, 30000)
    assert out["partial"] and out["data"]["products"][0] == PRODUCTS["products"][0] and out["left_out_items"] > 0
    assert len(json.dumps(out)) <= 31000 and "too_large" not in out


async def test_the_agent_gets_the_whole_answer_through_call_tool():
    async def fake_call(slug, tool, args):
        return PRODUCTS

    conn = SimpleNamespace(slug="gw", name="Gateway", alias="", kind="mcp", provider="mcp", server_url="https://gw.test/mcp",
                           server_key="mcp:gw", id="c1", health="ok")  # fmt: skip
    ctx = deep.RunContext(db=None, user=SimpleNamespace(id="u1"), connectors={"gw": conn}, auto_confirm=True, call_impl=fake_call)
    ctx.gateway_surface["gw"] = {"search": None, "execute": None, "resolve": None}
    out = await deep.call_user_tool(ctx, "gw", "get_product_sales", {"brand": "x"})
    assert out == PRODUCTS


def test_earlier_connector_results_stay_whole_in_the_turn():
    big = json.dumps(PRODUCTS)
    msgs = [HumanMessage(content="One8 vs Lotto product sales"),
            AIMessage(content="", tool_calls=[{"id": "a", "name": "call_tool", "args": {}}]), ToolMessage(content=big, tool_call_id="a", name="call_tool"),
            AIMessage(content="", tool_calls=[{"id": "b", "name": "call_tool", "args": {}}]), ToolMessage(content=big, tool_call_id="b", name="call_tool"),
            AIMessage(content="comparing"), HumanMessage(content="go")]  # fmt: skip
    out, saved = context_trim.trim_messages(msgs)
    assert out[2].content == big and saved == 0
