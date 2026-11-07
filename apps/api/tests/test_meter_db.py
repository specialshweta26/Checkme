"""The per-user cost meter, against a real Postgres: model calls are counted for the user and feature whose work ran,
counters from several flushes add up, users never see each other's usage, and over the daily background budget the
inbox falls back to rules only.

Skipped unless CHECKME_TEST_DATABASE_URL is set (see tests/test_user_memory_db.py).
"""

from __future__ import annotations

import json
import os
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from tests.test_user_memory_db import _user_with_agents, factory  # noqa: F401 - the fixture is used by name

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")


@pytest.fixture(autouse=True)
def clean_meter():
    from app.agent import meter

    meter._pending.clear()
    meter._budget_cache.clear()
    yield
    meter._pending.clear()


def _client(factory, user):
    from app.auth.routes import me_router
    from app.db import get_db
    from app.deps import current_user

    app = FastAPI()
    app.include_router(me_router, prefix="/api")

    async def db_dep():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db] = db_dep
    app.dependency_overrides[current_user] = lambda: user
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app.test")


async def test_usage_is_counted_per_user_and_feature_and_adds_up(factory, monkeypatch):
    from app.agent import meter, usage

    monkeypatch.setattr(usage, "prices", lambda: {"big": [1.0, 4.0, 0.1]})  # USD per 1M tokens
    a, _ = await _user_with_agents(factory)
    b, _ = await _user_with_agents(factory)
    with meter.metered(a.id, "inbox"):
        usage.add("big", 1_000_000, 100_000, 500_000)
        meter.count("search")
    usage.add("big", 999, 999, 0)  # nobody's work: not metered
    assert await meter.flush(factory) == 2
    with meter.metered(a.id, "inbox"):
        usage.add("big", 1_000_000, 0, 0)  # a second flush adds to the same row
    with meter.metered(b.id, "chat"):
        usage.add("big", 10, 10, 0)
    await meter.flush(factory)
    with meter.metered(a.id, "chat"):
        usage.add("big", 5, 5, 0)  # not flushed yet: still shown
    async with _client(factory, a) as http:
        data = (await http.get("/api/me/usage")).json()
    inbox = data["by_feature"]["inbox"]
    assert (inbox["calls"], inbox["input"], inbox["cached"], inbox["output"], inbox["searches"]) == (2, 2_000_000, 500_000, 100_000, 1)
    # 1.5M uncached × $1 + 0.5M cached × $0.10 + 0.1M out × $4 = $1.95
    assert inbox["cost_usd"] == pytest.approx(1.95) and data["priced"] is True
    assert data["by_feature"]["chat"]["input"] == 5  # b's chat is not in a's usage
    assert data["background_today"]["tokens"] == 2_100_000


async def test_over_the_background_budget_the_inbox_uses_rules_only(factory, monkeypatch):
    from app.agent import meter, usage
    from app.config import get_settings
    from app.inbox import service

    a, _ = await _user_with_agents(factory)
    monkeypatch.setattr(get_settings(), "daily_background_token_budget", 1000)
    async with factory() as db:
        assert await meter.over_budget(db, a.id) is False
    meter._budget_cache.clear()
    with meter.metered(a.id, "memory"):
        usage.add("small", 900, 200, 0)
    async with factory() as db:
        assert await meter.over_budget(db, a.id) is True

    calls = []

    async def fake_llm(*args, **kwargs):
        calls.append(1)
        return json.dumps({"classification": "reply", "summary": "s", "key_points": [], "draft": "d"})

    monkeypatch.setattr(service, "_llm", fake_llm)

    async def no_ctx(db, user, items):
        return {}

    monkeypatch.setattr("app.inbox.memory_context.contexts_for", no_ctx)

    def card(sender):
        return SimpleNamespace(id=uuid.uuid4(), connector_id="c1", sender=sender, subject="Hi", snippet="p", cc_header="",
                               classification="", summary="", key_points=[], draft="", ai_draft="", key_person=False, sender_note="")

    class Db:
        async def commit(self):
            pass

    alert, person = card("Alerts <alerts@bank.test>"), card("Priya <priya@acme.test>")
    await service._classify_many(Db(), a, [SimpleNamespace(id="c1")], [alert, person], rules_only=True)
    assert calls == [] and alert.classification == "discard" and person.classification == ""  # waits for tomorrow
