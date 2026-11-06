"""Tokens and cost per chat turn (docs/LLD.md "Turn usage")."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.agent import usage
from app.config import get_settings


def test_tally_adds_up_per_model_and_nested_runs_join_the_outer_one():
    tally, token = usage.start()
    try:
        usage.add("gpt-6-luna", 1000, 200, 400)
        usage.add("gpt-6-luna", 500, 100)
        inner, inner_token = usage.start()  # a nested run (teammate, healing step)
        assert inner is tally and inner_token is None
        usage.add("gpt-5.4-nano", 300, 50)
        usage.add("ignored", 0, 0)
    finally:
        usage.end(token)
    assert tally == {
        "gpt-6-luna": {"input": 1500, "output": 300, "cached": 400, "calls": 2},
        "gpt-5.4-nano": {"input": 300, "output": 50, "cached": 0, "calls": 1},
    }
    usage.add("after", 10, 10)  # no turn running: nothing to add to
    assert "after" not in tally


def test_summary_prices_known_models_and_flags_the_rest():
    tally = {"gpt-6-luna-2026-10": {"input": 2_000_000, "output": 100_000, "cached": 1_000_000, "calls": 3},
             "mystery": {"input": 10, "output": 5, "cached": 0, "calls": 1}}  # fmt: skip
    out = usage.summary(tally, {"gpt-6-luna": [2.0, 8.0, 0.5]})
    # 1M uncached * $2 + 1M cached * $0.5 + 0.1M out * $8 = $3.30
    assert out["cost_usd"] == pytest.approx(3.3) and out["unpriced"] == ["mystery"]
    assert out["input"] == 2_000_010 and out["output"] == 100_005 and out["cached"] == 1_000_000
    assert usage.summary(tally, {})["cost_usd"] is None


def test_prices_come_from_settings(monkeypatch):
    monkeypatch.setattr(get_settings(), "model_prices", '{"gpt-5.4-nano": [0.05, 0.4]}')
    assert usage.prices() == {"gpt-5.4-nano": [0.05, 0.4]}
    monkeypatch.setattr(get_settings(), "model_prices", "not json")
    assert usage.prices() == {}


def test_reading_openai_and_langchain_usage():
    tally, token = usage.start()
    try:
        usage.from_openai("gpt-5.4-nano", SimpleNamespace(input_tokens=120, output_tokens=30, input_tokens_details=SimpleNamespace(cached_tokens=20)))
        usage.from_openai("text-embedding-3-small", {"prompt_tokens": 64, "total_tokens": 64})
        msg = SimpleNamespace(usage_metadata={"input_tokens": 900, "output_tokens": 70, "input_token_details": {"cache_read": 300}},
                              response_metadata={"model_name": "gpt-6-luna"})  # fmt: skip
        usage.from_langchain(SimpleNamespace(llm_output={}, generations=[[SimpleNamespace(message=msg)]]))
        usage.from_langchain(SimpleNamespace(llm_output={"model_name": "gpt-6-luna", "token_usage": {"prompt_tokens": 10, "completion_tokens": 2}}, generations=[]))
        usage.from_langchain(object())  # never raises
    finally:
        usage.end(token)
    assert tally["gpt-5.4-nano"] == {"input": 120, "output": 30, "cached": 20, "calls": 1}
    assert tally["gpt-6-luna"] == {"input": 910, "output": 72, "cached": 300, "calls": 2}
    assert tally["text-embedding-3-small"] == {"input": 64, "output": 0, "cached": 0, "calls": 1}  # input only


async def test_a_chat_turn_returns_and_stores_its_usage(monkeypatch):
    from tests.test_stop import FakeDB, FakeRedis

    from app.agent import cancel as run_cancel
    from app.chat import routes as chat_routes

    redis = FakeRedis()
    import app.cache

    monkeypatch.setattr(app.cache, "get_redis", lambda: redis)
    agent = SimpleNamespace(id="a1", name="tony", avatar="", color="")

    async def roster(*a):
        return agent, None, []

    async def nothing(*a, **k):
        return None

    async def not_limited(*a, **k):
        return False

    async def fake_agent(db, user, session_id, request_id, history, query, confirms, **kw):
        usage.add("gpt-6-luna", 1200, 80)
        await asyncio.sleep(0)
        return {"text": "Here you go.", "used_tools": []}

    monkeypatch.setattr(chat_routes, "rate_limited", not_limited)
    monkeypatch.setattr(chat_routes, "_resolve_roster", roster)
    monkeypatch.setattr(chat_routes, "_maybe_start_job", nothing)
    monkeypatch.setattr(chat_routes, "run_deep_agent", fake_agent)
    monkeypatch.setattr(get_settings(), "model_prices", '{"gpt-6-luna": [2.0, 8.0]}')
    db = FakeDB()
    user = SimpleNamespace(id="u1", letta_agent_id="x", timezone="Asia/Kolkata")
    body = chat_routes.ChatIn(message="What is on my calendar tomorrow?")
    out = await chat_routes.chat(body, SimpleNamespace(add_task=lambda *a, **k: None), SimpleNamespace(headers={}), user, db)
    assert out["usage"]["input"] == 1200 and out["usage"]["output"] == 80
    assert out["usage"]["cost_usd"] == pytest.approx((1200 * 2 + 80 * 8) / 1_000_000)
    stored = [r for r in db.rows if getattr(r, "role", None) == "assistant"][-1]
    assert stored.meta["usage"]["input"] == 1200
    run_cancel._RUNS.clear()
