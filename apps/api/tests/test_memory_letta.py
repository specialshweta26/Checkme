import time
from types import SimpleNamespace

import httpx
import pytest

from app.agent import memory
from app.connectors.platform import platform_needs_sync


class _Boom:
    async def post(self, *args, **kwargs):
        raise httpx.ConnectError("letta down")

    async def get(self, *args, **kwargs):
        raise httpx.ConnectError("letta down")


@pytest.mark.asyncio
async def test_ensure_letta_fails_fast_and_skips(monkeypatch):
    memory._letta_skip_until = 0.0
    user = SimpleNamespace(id="u1", display_name="A", email="a@b.c", letta_agent_id=None)
    monkeypatch.setattr(
        memory,
        "get_settings",
        lambda: SimpleNamespace(letta_base_url="http://letta:8283", letta_api_key="", letta_agent_model="", openai_model="x"),
    )
    monkeypatch.setattr(memory, "shared_client", lambda: _Boom())
    t0 = time.monotonic()
    assert await memory.ensure_letta_agent(user) is None
    first = time.monotonic() - t0
    assert first < 1.5
    t1 = time.monotonic()
    assert await memory.ensure_letta_agent(user) is None
    assert time.monotonic() - t1 < 0.05


def test_platform_skips_fresh_sync():
    now = SimpleNamespace(health="ok", last_synced_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    assert platform_needs_sync(now) is False
    stale = SimpleNamespace(health="pending", last_synced_at=None)
    assert platform_needs_sync(stale) is True
