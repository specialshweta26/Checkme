"""Faster turns and fewer billed tokens: checks run side by side, a prompt cache key, a stable tool list."""

from __future__ import annotations

import asyncio
import time
import uuid
from types import SimpleNamespace

from app.agent import deep, tool_scope


def test_cache_key_is_per_agent_and_hides_the_id():
    agent = SimpleNamespace(id=uuid.uuid4())
    user = SimpleNamespace(id=uuid.uuid4())
    k1 = deep.cache_key(SimpleNamespace(agent=agent, user=user))
    assert k1.startswith("lumio-") and str(agent.id) not in k1
    assert k1 == deep.cache_key(SimpleNamespace(agent=agent, user=SimpleNamespace(id=uuid.uuid4())))
    assert deep.cache_key(SimpleNamespace(agent=None, user=user)) not in ("", k1)
    assert deep.cache_key(None) == ""


def test_chat_model_sends_the_cache_key(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "openai_api_key", "sk-test")
    ctx = SimpleNamespace(agent=SimpleNamespace(id=uuid.uuid4()), user=None)
    model = deep._chat_model(ctx)
    assert model.model_kwargs.get("prompt_cache_key") == deep.cache_key(ctx)
    monkeypatch.setattr(get_settings(), "prompt_cache_key", False)
    assert "prompt_cache_key" not in (deep._chat_model(ctx).model_kwargs or {})


class FakeRedis:
    def __init__(self):
        self.sets: dict[str, set] = {}

    async def smembers(self, key):
        return {x.encode() for x in self.sets.get(key, set())}

    async def sadd(self, key, *vals):
        self.sets.setdefault(key, set()).update(vals)

    async def expire(self, key, ttl):
        return True


async def test_tool_groups_once_shown_stay_shown(monkeypatch):
    import app.cache as cache

    fake = FakeRedis()
    monkeypatch.setattr(cache, "get_redis", lambda: fake)
    groups = list(tool_scope.GROUPS)
    sid = uuid.uuid4()
    first = await tool_scope.sticky(sid, groups[1:])  # turn 1 needed the first group
    assert first == groups[1:]
    second = await tool_scope.sticky(sid, groups)  # turn 2 needs none: the first stays, so the tool list is unchanged
    assert second == groups[1:]
    third = await tool_scope.sticky(sid, [g for g in groups if g != groups[2]])
    assert groups[2] not in third and groups[0] not in third
    assert await tool_scope.sticky(uuid.uuid4(), groups) == groups  # another chat starts lean
    assert await tool_scope.sticky(sid, groups, skip=True) == groups


async def test_sticky_without_redis_scopes_each_turn(monkeypatch):
    import app.cache as cache

    def broken():
        raise RuntimeError("down")

    monkeypatch.setattr(cache, "get_redis", broken)
    assert await tool_scope.sticky(uuid.uuid4(), ["browser"]) == ["browser"]


async def test_integrity_and_role_checks_run_side_by_side(monkeypatch):
    async def slow_integrity(query, history):
        await asyncio.sleep(0.3)
        return None

    async def slow_role(db, user, agent, query, history):
        await asyncio.sleep(0.3)
        return {"text": "Ask Ops Analyst.", "role_redirect": {}}

    async def no_budget(db, agent):
        return None

    monkeypatch.setattr(deep, "_integrity_block", slow_integrity)
    monkeypatch.setattr(deep, "_role_block", slow_role)
    monkeypatch.setattr(deep, "_budget_block", no_budget)
    agent = SimpleNamespace(id=None)
    t = time.monotonic()
    out = await deep.run_deep_agent(None, SimpleNamespace(id=uuid.uuid4()), uuid.uuid4(), "r", [], "sales?", workspace_agent=agent)
    assert out["text"] == "Ask Ops Analyst." and time.monotonic() - t < 0.5


async def test_an_integrity_refusal_still_wins(monkeypatch):
    async def refuse(query, history):
        await asyncio.sleep(0.05)
        return {"text": "I can't change the figures.", "integrity_refused": {"kind": "scale"}}

    async def redirect(db, user, agent, query, history):
        return {"text": "Ask Ops Analyst."}

    async def no_budget(db, agent):
        return None

    monkeypatch.setattr(deep, "_integrity_block", refuse)
    monkeypatch.setattr(deep, "_role_block", redirect)
    monkeypatch.setattr(deep, "_budget_block", no_budget)
    out = await deep.run_deep_agent(None, SimpleNamespace(id=uuid.uuid4()), uuid.uuid4(), "r", [], "x5 the sales",
                                    workspace_agent=SimpleNamespace(id=None))  # fmt: skip
    assert out["text"] == "I can't change the figures."
