"""Actions that leave the app happen once per exact content, even when retried."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.connectors import idempotency


class FakeRedis:
    def __init__(self):
        self.data = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    async def get(self, key):
        return self.data.get(key)

    async def delete(self, key):
        self.data.pop(key, None)


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr("app.cache.get_redis", lambda: fake)
    return fake


def test_which_actions_are_guarded():
    assert idempotency.is_side_effect("reply", builtin_mail=True)
    assert idempotency.is_side_effect("create_teams_meeting", builtin_mail=True)
    assert not idempotency.is_side_effect("list_messages", builtin_mail=True)
    assert idempotency.is_side_effect("GMAIL_SEND_EMAIL", builtin_mail=False)
    assert idempotency.is_side_effect("slack.post_message", builtin_mail=False)
    assert not idempotency.is_side_effect("search_messages", builtin_mail=False)
    assert not idempotency.is_side_effect("sender_lookup", builtin_mail=False)  # "send" inside a word doesn't count


async def test_a_retry_after_success_does_not_send_again(redis):
    sent = []

    async def send():
        sent.append(1)
        return {"ok": True, "message_id": "m1"}

    args = {"message_id": "x", "body": "Thanks, Friday works."}
    first = await idempotency.once("u1", "outlook", "reply", args, send)
    again = await idempotency.once("u1", "outlook", "reply", args, send)
    assert sent == [1] and first == {"ok": True, "message_id": "m1"}
    assert again["duplicate"] is True and again["message_id"] == "m1" and "not repeated" in again["note"]
    # Different content, or another user, is a different action.
    await idempotency.once("u1", "outlook", "reply", {**args, "body": "Thanks, Monday works."}, send)
    await idempotency.once("u2", "outlook", "reply", args, send)
    assert sent == [1, 1, 1]


async def test_two_sends_at_once_send_one(redis):
    sent = []

    async def slow():
        sent.append(1)
        await asyncio.sleep(0.05)
        return {"ok": True}

    results = await asyncio.gather(*(idempotency.once("u1", "gmail", "send_message", {"to": "a@b.c", "body": "hi"}, slow) for _ in range(3)))
    assert sent == [1] and sum(1 for r in results if r.get("ok") and not r.get("duplicate")) == 1
    assert sum(1 for r in results if "already being sent" in (r.get("error") or "")) == 2


async def test_a_timeout_blocks_a_blind_retry_but_a_refusal_does_not(redis):
    async def timeout():
        raise asyncio.TimeoutError()

    with pytest.raises(asyncio.TimeoutError):
        await idempotency.once("u1", "outlook", "reply", {"body": "a"}, timeout)
    blocked = await idempotency.once("u1", "outlook", "reply", {"body": "a"}, timeout)
    assert "may have gone through" in blocked["error"] and "Sent folder" in blocked["error"]

    calls = []

    async def refused():
        calls.append(1)
        return {"error": "Recipient address rejected"}

    assert (await idempotency.once("u1", "outlook", "reply", {"body": "b"}, refused))["error"].startswith("Recipient")
    assert (await idempotency.once("u1", "outlook", "reply", {"body": "b"}, refused))["error"].startswith("Recipient")
    assert calls == [1, 1]  # nothing was sent, so the retry really tried again

    async def bad_request():
        raise ValueError("400 Bad Request: invalid recipient")

    with pytest.raises(ValueError):
        await idempotency.once("u1", "outlook", "reply", {"body": "c"}, bad_request)
    assert idempotency.key_for("u1", "outlook", "reply", {"body": "c"}) not in redis.data  # released


async def test_without_redis_the_send_still_happens(monkeypatch):
    def broken():
        raise ConnectionError("redis down")

    monkeypatch.setattr("app.cache.get_redis", broken)

    async def send():
        return {"ok": True}

    assert await idempotency.once("u1", "outlook", "reply", {"body": "a"}, send) == {"ok": True}


async def test_the_runtime_guards_sends_but_not_reads(monkeypatch, redis):
    from types import SimpleNamespace

    from app.connectors.runtime import RuntimeConnector

    row = SimpleNamespace(id="c1", kind="builtin", provider="outlook", alias="", name="Outlook", slug="outlook", user_id="u1")
    rt = RuntimeConnector(row)
    calls = []

    async def raw(name, args):
        calls.append(name)
        return {"ok": True}

    monkeypatch.setattr(rt, "_call", raw)
    for _ in range(2):
        await rt.call("reply", {"body": "x"})
        await rt.call("list_messages", {"limit": 5})
    assert calls == ["reply", "list_messages", "list_messages"]
    assert json.loads(next(iter(redis.data.values())))["state"] == "done"
