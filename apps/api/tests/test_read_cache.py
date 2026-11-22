"""Read-only tool results are reused for a couple of minutes, per user, and never after a write (app/agent/read_cache.py)."""

from __future__ import annotations

import uuid

import pytest

from app.agent import read_cache


class FakeRedis:
    def __init__(self):
        self.kv: dict[str, bytes] = {}

    async def get(self, k):
        return self.kv.get(k)

    async def set(self, k, v, ex=None):
        self.kv[k] = v.encode() if isinstance(v, str) else v

    async def incr(self, k):
        n = int(self.kv.get(k, b"0")) + 1
        self.kv[k] = str(n).encode()
        return n

    async def expire(self, k, ttl):
        return True


@pytest.fixture
def fake(monkeypatch):
    import app.cache as cache

    f = FakeRedis()
    monkeypatch.setattr(cache, "get_redis", lambda: f)
    return f


async def test_a_read_is_reused_per_user_and_exact_args(fake):
    u, other = uuid.uuid4(), uuid.uuid4()
    await read_cache.put(u, "shopify", "list_orders", {"days": 3}, {"orders": [1, 2]})
    assert await read_cache.get(u, "shopify", "list_orders", {"days": 3}) == {"orders": [1, 2]}
    assert await read_cache.get(u, "shopify", "list_orders", {"days": 4}) is None
    assert await read_cache.get(other, "shopify", "list_orders", {"days": 3}) is None  # never across users


async def test_a_write_to_the_connector_makes_older_reads_stale(fake):
    u = uuid.uuid4()
    await read_cache.put(u, "gmail", "list_threads", {}, {"threads": ["a"]})
    await read_cache.wrote(u, "gmail")
    assert await read_cache.get(u, "gmail", "list_threads", {}) is None
    await read_cache.put(u, "gmail", "list_threads", {}, {"threads": ["a", "b"]})
    assert await read_cache.get(u, "gmail", "list_threads", {}) == {"threads": ["a", "b"]}


async def test_live_screens_errors_and_big_results_are_never_kept(fake, monkeypatch):
    u = uuid.uuid4()
    await read_cache.put(u, "computer", "screenshot", {}, {"img": "x"})
    assert await read_cache.get(u, "computer", "screenshot", {}) is None
    await read_cache.put(u, "crm", "get", {}, {"error": "timeout"})
    assert await read_cache.get(u, "crm", "get", {}) is None
    await read_cache.put(u, "crm", "dump", {}, {"rows": "x" * (read_cache.MAX_BYTES + 1)})
    assert await read_cache.get(u, "crm", "dump", {}) is None
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "read_cache_s", 0)
    await read_cache.put(u, "crm", "get", {"id": 1}, {"ok": 1})
    assert await read_cache.get(u, "crm", "get", {"id": 1}) is None


async def test_redis_down_just_runs_the_tool(monkeypatch):
    import app.cache as cache

    def broken():
        raise RuntimeError("down")

    monkeypatch.setattr(cache, "get_redis", broken)
    u = uuid.uuid4()
    await read_cache.put(u, "crm", "get", {}, {"ok": 1})
    await read_cache.wrote(u, "crm")
    assert await read_cache.get(u, "crm", "get", {}) is None
