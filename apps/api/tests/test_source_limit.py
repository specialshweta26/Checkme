"""A data source whose own usage limit is used up ("Organization daily data volume limit exceeded"): Lumio stops asking
it for a while and tells the agent plainly (app/agent/source_limit.py), and reads of closed days are kept longer so
asking again doesn't spend the allowance (app/agent/read_cache.py ``ttl_for``)."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.agent import deep, read_cache, source_limit
from app.config import get_settings

SAID = "Error: Organization daily data volume limit exceeded. Contact your admin."


class FakeRedis:
    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value.encode() if isinstance(value, str) else value

    async def incr(self, key):
        self.data[key] = int(self.data.get(key) or 0) + 1

    async def expire(self, key, seconds):
        return True


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr("app.cache.get_redis", lambda: fake)
    return fake


def test_limit_messages_are_recognised_and_others_are_not():
    assert "daily data volume limit exceeded" in source_limit.said({"error": SAID})
    assert source_limit.said(RuntimeError("Monthly quota exceeded for this plan"))
    assert source_limit.said({"content": [{"type": "text", "text": "Daily request limit reached."}]})
    assert source_limit.said({"error": "quota exhausted"})
    assert not source_limit.said({"error": "Rate limit: slow down and retry in 2s"})
    assert not source_limit.said({"summary": {"limit": 100, "exceeded": False}})
    assert not source_limit.said({"error": "brand not found"})


def test_the_pause_ends_at_midnight_or_after_an_hour():
    tz = ZoneInfo("Asia/Kolkata")
    late = datetime(2026, 11, 23, 23, 40, tzinfo=tz)
    assert source_limit.pause_until("Asia/Kolkata", late) == datetime(2026, 11, 24, 0, 0, tzinfo=tz)
    noon = datetime(2026, 11, 23, 12, 0, tzinfo=tz)
    assert source_limit.pause_until("Asia/Kolkata", noon) == noon + timedelta(hours=1)
    just_before = datetime(2026, 11, 23, 23, 59, 30, tzinfo=tz)
    assert source_limit.pause_until("Asia/Kolkata", just_before) == just_before + timedelta(minutes=5)


def _gateway(monkeypatch, answers):
    calls = []

    async def invoke(db, user, conn, tool_name, args, attachments=None):
        name = args.get("tool_name") or tool_name  # the gateway's execute names the action inside
        calls.append(name)
        return answers(name)

    async def surface(ctx, conn):
        return {"search": "gateway__tool_search", "execute": "gateway__execute", "resolve": None}

    async def params(ctx, conn, tool_name):
        return {"fields": ["brand", "start", "end"], "required": ["brand"]}

    monkeypatch.setattr(deep, "invoke_named", invoke)
    monkeypatch.setattr(deep, "_load_gateway_surface", surface)
    monkeypatch.setattr(deep, "gateway_action_args", params)
    monkeypatch.setattr(deep, "check_gateway_args", lambda known, tool, args: None)
    conn = SimpleNamespace(kind="mcp", server_url="https://mcp-gateway.example.test/mcp", alias="", name="Sales",
                           server_key="mcp:sales", id="c1", health="ok", slug="sales")  # fmt: skip
    user = SimpleNamespace(id="u1", timezone="Asia/Kolkata")
    return calls, (lambda: deep.RunContext(db=None, user=user, connectors={"sales": conn}, query="one8 sales yesterday"))


async def test_after_the_source_says_its_limit_is_used_up_lumio_stops_asking(monkeypatch, redis):
    calls, ctx = _gateway(monkeypatch, lambda tool: {"error": SAID})
    first = await deep.call_user_tool(ctx(), "sales", "get_sales_dashboard", {"brand": "one8"})
    assert first["source_limit"] and "daily data volume limit exceeded" in first["error"]
    assert "not to Lumio or the model" in first["_lumio_note"] and "Don't try other actions" in first["_lumio_note"]
    second = await deep.call_user_tool(ctx(), "sales", "get_data_daily", {"brand": "one8"})  # another action, next turn
    assert second["source_limit"] and calls == ["get_sales_dashboard"]


async def test_a_source_that_raises_its_limit_is_paused_too(monkeypatch, redis):
    from app.connectors.runtime import ToolError

    def boom(tool):
        raise ToolError(SAID)

    calls, ctx = _gateway(monkeypatch, boom)
    assert (await deep.call_user_tool(ctx(), "sales", "get_sales_dashboard", {"brand": "one8"}))["source_limit"]
    assert (await deep.call_user_tool(ctx(), "sales", "get_sales_split", {"brand": "one8"}))["source_limit"]
    assert calls == ["get_sales_dashboard"]


async def test_ordinary_errors_dont_pause_the_source(monkeypatch, redis):
    calls, ctx = _gateway(monkeypatch, lambda tool: {"error": "brand not found"})
    await deep.call_user_tool(ctx(), "sales", "get_sales_dashboard", {"brand": "x"})
    await deep.call_user_tool(ctx(), "sales", "get_sales_dashboard", {"brand": "y"})
    assert len(calls) == 2


# --- closed days are kept longer -------------------------------------------------------------------------------------


def test_reads_of_closed_days_are_kept_for_hours(monkeypatch):
    monkeypatch.setattr(get_settings(), "read_cache_s", 120)
    monkeypatch.setattr(get_settings(), "read_cache_past_s", 21600)
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    yesterday, week_ago = (today - timedelta(days=1)).isoformat(), (today - timedelta(days=7)).isoformat()
    assert read_cache.ttl_for({"start": week_ago, "end": yesterday}, "Asia/Kolkata") == 21600
    assert read_cache.ttl_for({"tool_name": "x", "arguments": {"date": yesterday}}, "Asia/Kolkata") == 21600
    assert read_cache.ttl_for({"tool_name": "x", "arguments": '{"end_date": "%s"}' % yesterday}, "Asia/Kolkata") == 21600
    assert read_cache.ttl_for({"start": week_ago, "end": today.isoformat()}, "Asia/Kolkata") == 120  # includes today
    assert read_cache.ttl_for({"brand": "one8"}, "Asia/Kolkata") == 120  # no dates: could be live
    assert read_cache.ttl_for({"end": "last week"}, "Asia/Kolkata") == 120  # not a date Lumio can read
    monkeypatch.setattr(get_settings(), "read_cache_s", 0)
    assert read_cache.ttl_for({"end": yesterday}, "Asia/Kolkata") == 0  # the cache is off
