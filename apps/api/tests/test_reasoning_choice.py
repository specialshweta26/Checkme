"""Reasoning: picked for any model (docs/LLD.md "Reasoning"), and raised on its own once a turn works with data."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app import llm_gateway
from app.agent import data_reasoning
from app.config import get_settings


def _spec(provider: str, reasoning: str = "", model: str = "m-1", base_url: str = ""):
    return llm_gateway.ModelSpec(provider=provider, model=model, api_key="test-key", base_url=base_url, source="user", reasoning=reasoning)


def test_openai_takes_the_picked_effort():
    chat = llm_gateway.chat_model(_spec("openai", "high", model="gpt-test-reasoning"))
    assert (getattr(chat, "reasoning", None) or {}).get("effort") == "high" or getattr(chat, "reasoning_effort", None) == "high"


def test_anthropic_gets_extended_thinking_with_room_to_answer():
    pytest.importorskip("langchain_anthropic")
    chat = llm_gateway.chat_model(_spec("anthropic", "medium", model="claude-test"))
    assert chat.thinking == {"type": "enabled", "budget_tokens": llm_gateway.THINKING_BUDGET["medium"]}
    assert chat.max_tokens > llm_gateway.THINKING_BUDGET["medium"]
    plain = llm_gateway.chat_model(_spec("anthropic", "", model="claude-test"))
    assert not getattr(plain, "thinking", None)


def test_compatible_providers_get_reasoning_effort():
    provider = next(iter(llm_gateway.COMPATIBLE))
    chat = llm_gateway.chat_model(_spec(provider, "low", base_url="https://example.test/v1"))
    assert getattr(chat, "reasoning_effort", None) == "low"
    off = llm_gateway.chat_model(_spec(provider, "none", base_url="https://example.test/v1"))
    assert not getattr(off, "reasoning_effort", None)


def test_unknown_levels_are_ignored():
    assert llm_gateway.picked_reasoning(_spec("openai", "extreme")) == ""
    assert llm_gateway.picked_reasoning(None) == ""


def test_data_work_raises_reasoning_only_after_a_data_tool(monkeypatch):
    monkeypatch.setattr(get_settings(), "data_reasoning_effort", "medium")
    monkeypatch.setattr(get_settings(), "openai_reasoning_effort", "none")
    quick = [HumanMessage(content="hi")]
    assert data_reasoning.settings_for("gpt-test", quick) is None
    data = [HumanMessage(content="top article"), AIMessage(content="", tool_calls=[{"id": "1", "name": "call_tool", "args": {}}])]
    assert data_reasoning.settings_for("gpt-test", data) == {"reasoning": {"effort": "medium", "summary": "auto"}}
    monkeypatch.setattr(get_settings(), "data_reasoning_effort", "")
    assert data_reasoning.settings_for("gpt-test", data) is None


async def test_a_picked_level_is_kept_by_the_data_middleware(monkeypatch):
    monkeypatch.setattr(get_settings(), "data_reasoning_effort", "medium")
    seen = {}

    class Request:
        def __init__(self, messages, model_settings=None):
            self.messages, self.model_settings = messages, model_settings or {}

        def override(self, **kw):
            return Request(kw.get("messages", self.messages), kw.get("model_settings", self.model_settings))

    async def handler(req):
        seen["settings"] = req.model_settings
        return "ok"

    data = [AIMessage(content="", tool_calls=[{"id": "1", "name": "search_tools", "args": {}}])]
    await data_reasoning.middleware("gpt-test", True).awrap_model_call(Request(data), handler)
    assert seen["settings"]["reasoning"]["effort"] == "medium"
    await data_reasoning.middleware("gpt-test", True, picked="low").awrap_model_call(Request(data), handler)
    assert "reasoning" not in seen["settings"]
    await data_reasoning.middleware("claude-test", False).awrap_model_call(Request(data), handler)
    assert "reasoning" not in seen["settings"]


async def test_lumios_own_model_can_carry_a_reasoning_level(monkeypatch):
    from app.model_keys import service

    row = SimpleNamespace(key_id=None, scope="user", purpose="agent", model="", tier="", reasoning="high")

    async def choices(db, user_id, scope):
        return [row]

    async def keys(db, user_id):
        return []

    monkeypatch.setattr(service, "_choices", choices)
    monkeypatch.setattr(service, "_keys", keys)
    out = await service.load_all_choices(None, "u1")
    spec = out["user"]["agent"]
    assert spec.reasoning == "high" and spec.is_platform and spec.model == llm_gateway.resolve(None, "agent").model
