"""A new agent's first-chat ideas (app/agents/onboarding.py): made once when it's created, then served from cache."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

from app.agents import onboarding


class FakeRedis:
    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value


def _setup(monkeypatch, result):
    redis = FakeRedis()
    calls = []

    async def generate(agent):
        calls.append(agent.title)
        await asyncio.sleep(0.01)
        return result

    monkeypatch.setattr("app.cache.get_redis", lambda: redis)
    monkeypatch.setattr(onboarding, "generate_onboarding", generate)
    return redis, calls


IDEAS = {"greeting": "Hi", "questions": ["What first?"], "tasks": [{"title": "Draft", "prompt": "Draft the brief"}], "skills": []}


async def test_made_once_when_created_and_reused_by_the_chat(monkeypatch):
    redis, calls = _setup(monkeypatch, IDEAS)
    agent = SimpleNamespace(id=uuid.uuid4(), name="Marki", title="Marketing analyst", instructions="")
    onboarding.warm(agent)
    first = await onboarding.onboarding_for(agent)  # the chat opens while the warm-up is still running
    again = await onboarding.onboarding_for(agent)
    await asyncio.sleep(0.02)
    assert first == again == IDEAS and calls == ["Marketing analyst"]


async def test_a_new_title_gets_new_ideas(monkeypatch):
    _, calls = _setup(monkeypatch, IDEAS)
    agent = SimpleNamespace(id=uuid.uuid4(), name="Marki", title="Marketing analyst", instructions="")
    await onboarding.onboarding_for(agent)
    agent.title = "Recruiter"
    await onboarding.onboarding_for(agent)
    assert calls == ["Marketing analyst", "Recruiter"]


async def test_a_fallback_is_not_kept(monkeypatch):
    fallback = onboarding._fallback(SimpleNamespace(name="Marki", title=""))
    redis, calls = _setup(monkeypatch, fallback)
    agent = SimpleNamespace(id=uuid.uuid4(), name="Marki", title="", instructions="")
    await onboarding.onboarding_for(agent)
    await onboarding.onboarding_for(agent)
    assert len(calls) == 2 and redis.data == {}
