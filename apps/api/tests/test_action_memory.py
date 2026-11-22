"""The gateway action that answered a question is offered the next time a question like it comes
(app/agent/action_memory.py). Before, every data question started the catalog hunt from zero: 3-6 searches at 4-7 s
and several wrong actions, 11-30 model steps."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.agent import action_memory as am
from app.config import get_settings

SLUG = "mcp-gateway-agilitas"
ACTION = "funnel-analytics__get_daily_sales"


def _ctx(results=None, connectors=None, user_id=None):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id or uuid.uuid4()),
        results=results or [],
        connectors=connectors if connectors is not None else {SLUG: object()},
        gateway_args={(SLUG, ACTION): {"fields": ["brand_id", "since", "until"], "required": ["brand_id"], "read_only": True,
                                       "schema": {"type": "object"}}},  # fmt: skip
    )


def _row(data, action=ACTION, tool="gateway__execute"):
    return {"connector_slug": SLUG, "tool_name": tool, "args": {"tool_name": action, "arguments": {"brand_id": "one8"}}, "data": data}


def test_only_actions_that_returned_data_count():
    ctx = _ctx([_row({"rows": [1]}), _row({"error": "bad brand"}, action="ssa__get_data"), _row([], action="x__empty"),
                {"connector_slug": "gmail", "tool_name": "list_messages", "args": {}, "data": {"messages": [1]}}])  # fmt: skip
    assert am.answered(ctx) == [(SLUG, ACTION, {"brand_id": "one8"})]


def test_the_closest_questions_rank_first_and_only_connected_ones_show():
    entries = [
        {"slug": SLUG, "action": ACTION, "questions": ["What were total net sales yesterday?"], "uses": 3, "last": 1},
        {"slug": SLUG, "action": "inventory__stock_levels", "questions": ["Which products are lowest on stock?"], "uses": 9, "last": 2},
        {"slug": "gone", "action": "sales__daily", "questions": ["net sales by day"], "uses": 50, "last": 3},
    ]
    got = am.rank(entries, "Net sales for the last 3 days", {SLUG})
    assert [e["action"] for e in got] == [ACTION]
    assert am.rank(entries, "hi", {SLUG}) == []  # nothing shared, nothing offered


def test_preload_lets_call_tool_check_the_action_without_searching():
    ctx = _ctx()
    ctx.gateway_args = {}
    am.preload(ctx, [{"slug": SLUG, "action": ACTION, "params": {"fields": ["brand_id"], "read_only": True}}])
    assert ctx.gateway_args[(SLUG, ACTION)]["fields"] == ["brand_id"]
    assert ACTION in ctx.gateway_args[("__seen__", SLUG)]
    text = am.block([{"slug": SLUG, "action": ACTION, "questions": ["net sales yesterday"], "params": {"fields": ["brand_id"]}, "args": '{"brand_id": "one8"}'}])
    assert ACTION in text and "brand_id" in text and "call_tool" in text


async def _redis_ok():
    try:
        from app.cache import get_redis

        await get_redis().ping()
        return True
    except Exception:  # noqa: BLE001
        return False


async def test_remembered_then_recalled(monkeypatch):
    if not await _redis_ok():
        pytest.skip("no Redis")
    monkeypatch.setattr(get_settings(), "action_memory_days", 14)
    user_id = uuid.uuid4()
    await am.remember(_ctx([_row({"rows": [1]})], user_id=user_id), "What were total net sales yesterday?")
    await am.remember(_ctx([_row({"rows": [2]})], user_id=user_id), "Net sales for One8 this week")
    got = await am.recall(_ctx(user_id=user_id), "net sales for the last 3 days")
    assert got and got[0]["action"] == ACTION and got[0]["uses"] == 2
    assert got[0]["params"]["fields"] == ["brand_id", "since", "until"]
    assert got[0]["questions"][0] == "Net sales for One8 this week"
    assert await am.recall(_ctx(user_id=uuid.uuid4()), "net sales") == []  # another user's memory stays theirs
    monkeypatch.setattr(get_settings(), "action_memory_days", 0)
    assert await am.recall(_ctx(user_id=user_id), "net sales") == []  # off
    from app.cache import get_redis

    await get_redis().delete(am._key(user_id))
