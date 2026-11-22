"""Rollout controls: who may sign up, and the per-user daily cap on model use (docs/ROLLOUT.md)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.agent import meter
from app.auth import access
from app.config import get_settings


@pytest.fixture
def s(monkeypatch):
    s = get_settings()
    for k, v in (("signup_mode", "open"), ("signup_allow", ""), ("daily_user_budget_usd", 0.0),
                 ("daily_user_token_budget", 0), ("budget_exempt", ""), ("daily_background_budget_usd", 0.0),
                 ("daily_background_token_budget", 0)):  # fmt: skip
        monkeypatch.setattr(s, k, v)
    meter._cap_cache.clear()
    meter._budget_cache.clear()
    yield s
    meter._cap_cache.clear()
    meter._budget_cache.clear()


def test_sign_up_modes(s, monkeypatch):
    assert access.may_sign_up("anyone@x.com")
    monkeypatch.setattr(s, "signup_mode", "invite")
    monkeypatch.setattr(s, "signup_allow", "asha@x.com; @acme.com,\nbeta.io")
    assert access.may_sign_up("Asha@X.com") and access.may_sign_up("ravi@acme.com") and access.may_sign_up("a@beta.io")
    assert not access.may_sign_up("ravi@x.com") and not access.may_sign_up("a@notacme.com") and not access.may_sign_up("")
    assert not access.may_sign_up("a@sub.acme.com")  # domains match exactly
    monkeypatch.setattr(s, "signup_mode", "closed")
    assert not access.may_sign_up("asha@x.com")
    monkeypatch.setattr(s, "signup_mode", "opne")  # a typo closes to the allow list, never opens
    assert access.mode() == "invite" and not access.may_sign_up("anyone@x.com")


class _DB:
    def __init__(self, user):
        self.user = user

    async def get(self, _model, _id):
        return self.user


def _rows(monkeypatch, tokens: int, cost: float | None = 0.0, model: str = "gpt-5.4-mini", feature: str = "chat"):
    async def rows(db, user_id, since):
        return [{"feature": feature, "model": model, "input": tokens, "output": 0, "cost_usd": cost}]

    monkeypatch.setattr(meter, "usage_rows", rows)


async def test_daily_cap_counts_every_feature_and_spares_exempt_users(s, monkeypatch):
    user = SimpleNamespace(id=uuid.uuid4(), email="heavy@x.com")
    _rows(monkeypatch, 900_000)
    assert not await meter.over_user_cap(None, user)  # no cap set
    monkeypatch.setattr(s, "daily_user_token_budget", 1_000_000)
    assert not await meter.over_user_cap(None, user)
    meter._cap_cache.clear()
    _rows(monkeypatch, 1_200_000, feature="jobs")
    assert await meter.over_user_cap(None, user)
    meter._cap_cache.clear()
    monkeypatch.setattr(s, "budget_exempt", "owner@x.com, HEAVY@x.com")
    assert not await meter.over_user_cap(None, user)


async def test_daily_cap_in_dollars_ignores_search_and_tool_units(s, monkeypatch):
    user = SimpleNamespace(id=uuid.uuid4(), email="u@x.com")
    monkeypatch.setattr(s, "daily_user_budget_usd", 2.0)
    _rows(monkeypatch, 10_000_000, cost=None, model="search")
    assert not await meter.over_user_cap(None, user)
    meter._cap_cache.clear()
    _rows(monkeypatch, 10, cost=2.5)
    assert await meter.over_user_cap(None, user)


async def test_background_work_stops_at_the_daily_cap(s, monkeypatch):
    user = SimpleNamespace(id=uuid.uuid4(), email="u@x.com")
    _rows(monkeypatch, 5_000_000, feature="inbox")
    assert not await meter.over_budget(_DB(user), user.id)
    monkeypatch.setattr(s, "daily_user_token_budget", 1_000_000)
    assert await meter.over_budget(_DB(user), user.id)


async def test_chat_refuses_past_the_cap(s, monkeypatch):
    from fastapi import HTTPException

    from app.chat import routes

    async def not_limited(*a, **k):
        return False

    monkeypatch.setattr(routes, "rate_limited", not_limited)
    monkeypatch.setattr(s, "daily_user_token_budget", 1000)
    _rows(monkeypatch, 5000)
    user = SimpleNamespace(id=uuid.uuid4(), email="u@x.com")
    with pytest.raises(HTTPException) as e:
        await routes.chat(routes.ChatIn(message="hi"), None, None, user=user, db=None)
    assert e.value.status_code == 429 and e.value.detail == meter.CAP_MESSAGE
