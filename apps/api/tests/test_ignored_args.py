"""A tool that doesn't recognise an argument's name answers for its defaults (docs/LLD.md "Gateway arguments"): when its
answer covers a wider period than the dates sent, the agent is told, with the action's real parameter names."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent import deep, read_cache
from app.connectors import mcp_client


@pytest.fixture(autouse=True)
def _no_read_cache(monkeypatch):
    """A Redis left running on the machine would answer these calls from an earlier run."""

    async def miss(*a, **k):
        return None

    monkeypatch.setattr(read_cache, "get", miss)
    monkeypatch.setattr(read_cache, "put", miss)
    monkeypatch.setattr(read_cache, "wrote", miss)


def test_an_answer_for_other_dates_than_sent_is_caught():
    sent = {"tool_name": "get_catalog_performance", "arguments": {"brand": "x", "start": "2026-11-16", "end": "2026-11-17"}}
    got = {"connected": True, "range": {"start": "2026-10-19", "end": "2026-11-17"}, "rows": [{"units": 3}]}
    assert "2026-10-19" in mcp_client.ignored_dates(sent, got)
    assert not mcp_client.ignored_dates(sent, {"window": {"start": "2026-11-16", "end": "2026-11-17"}})
    assert not mcp_client.ignored_dates(sent, {"window": {"start": "2026-11-16", "end": "2026-11-16"}})  # clamped: fine
    assert not mcp_client.ignored_dates({"brand": "x"}, got)  # no dates sent: nothing to compare
    assert not mcp_client.ignored_dates(sent, {"rows": []})  # no window in the answer


async def test_ignored_dates_tell_the_agent_is_told_the_real_parameters(monkeypatch):
    calls = []

    async def invoke(db, user, conn, tool_name, args, attachments=None):
        calls.append(args)
        return {"connected": False, "range": {"start": "2026-10-19", "end": "2026-11-17"}}

    async def surface(ctx, conn):
        return {"search": "gateway__tool_search", "execute": "gateway__execute", "resolve": None}

    async def params(ctx, conn, tool_name):
        return {"fields": ["brand", "since", "until"], "required": ["brand"]} if tool_name == "get_catalog_performance" else None

    monkeypatch.setattr(deep, "invoke_named", invoke)
    monkeypatch.setattr(deep, "_load_gateway_surface", surface)
    monkeypatch.setattr(deep, "gateway_action_args", params)
    monkeypatch.setattr(deep, "check_gateway_args", lambda known, tool, args: None)  # let the wrong call through
    conn = SimpleNamespace(kind="mcp", server_url="https://mcp-gateway.example.test/mcp", alias="", name="Gateway",
                           server_key="mcp:gw", id="c1", health="ok", slug="gw")  # fmt: skip
    ctx = deep.RunContext(db=None, user=SimpleNamespace(id="u1"), connectors={"gw": conn}, query="product sales 16-17 Nov")
    out = await deep.call_user_tool(ctx, "gw", "get_catalog_performance", {"brand": "x", "start": "2026-11-16", "end": "2026-11-17"})
    # Asked once more under the catalog's names (start as since, end as until); still the wrong period, so the agent is told.
    assert [c["arguments"] for c in calls if "tool_name" in c][1] == {"brand": "x", "since": "2026-11-16", "until": "2026-11-17"}
    assert sum(1 for c in calls if "tool_name" in c) == 2 and out["args_ignored"] and out["arguments_expected"] == ["brand", "since", "until"]
    assert "brand, since, until" in out["note"]



def _gateway(monkeypatch, answer):
    calls, store = [], {}

    async def invoke(db, user, conn, tool_name, args, attachments=None):
        calls.append(args)
        return answer(args.get("arguments") or {})

    async def surface(ctx, conn):
        return {"search": "gateway__tool_search", "execute": "gateway__execute", "resolve": None}

    async def no_params(ctx, conn, tool_name):
        return None  # this gateway's catalog describes no parameters (agilitas, live)

    async def learned(user_id, slug, action):
        return dict(store.get((user_id, slug, action), {}))

    async def remember(user_id, slug, action, mapping):
        store[(user_id, slug, action)] = {**store.get((user_id, slug, action), {}), **mapping}

    from app.agent import arg_repair

    monkeypatch.setattr(deep, "invoke_named", invoke)
    monkeypatch.setattr(deep, "_load_gateway_surface", surface)
    monkeypatch.setattr(deep, "gateway_action_args", no_params)
    monkeypatch.setattr(arg_repair, "learned", learned)
    monkeypatch.setattr(arg_repair, "remember", remember)
    conn = SimpleNamespace(kind="mcp", server_url="https://mcp-gateway.example.test/mcp", alias="", name="Gateway",
                           server_key="mcp:gw", id="c1", health="ok", slug="gw")  # fmt: skip
    ctx = deep.RunContext(db=None, user=SimpleNamespace(id="u1"), connectors={"gw": conn}, query="One8 sales today")
    return ctx, calls, store


def _daily(inner):
    """funnel-analytics__get_daily_sales, live: takes brand, start, end; any other name falls back to 30 days."""
    start, end = inner.get("start", "2026-10-26"), inner.get("end", "2026-11-24")
    return {"brand": inner.get("brand", "all"), "window": {"start": start, "end": end}, "daily": {"days": [{"date": end, "net_sales": 1419088}]}}


async def test_dates_under_another_sources_names_are_sent_again_under_its_own(monkeypatch):
    ctx, calls, store = _gateway(monkeypatch, _daily)
    sent = {"brand": "one8", "from_date": "2026-11-24", "to_date": "2026-11-24"}
    out = await deep.call_user_tool(ctx, "gw", "funnel-analytics__get_daily_sales", sent)
    runs = [c["arguments"] for c in calls if "tool_name" in c]
    assert runs[-1] == {"brand": "one8", "start": "2026-11-24", "end": "2026-11-24"}
    assert out["window"] == {"start": "2026-11-24", "end": "2026-11-24"} and not out.get("args_ignored")
    assert "from_date as start" in out["_lumio_note"]
    assert store[("u1", "gw", "funnel-analytics__get_daily_sales")] == {"from_date": "start", "to_date": "end"}

    # The next call with the same wrong names goes out right the first time.
    calls.clear()
    out = await deep.call_user_tool(ctx, "gw", "funnel-analytics__get_daily_sales", {"brand": "one8", "from_date": "2026-11-23", "to_date": "2026-11-23"})
    assert [c["arguments"] for c in calls if "tool_name" in c] == [{"brand": "one8", "start": "2026-11-23", "end": "2026-11-23"}]
    assert out["window"]["start"] == "2026-11-23"


async def test_one_day_sent_as_date_becomes_the_sources_start_and_end(monkeypatch):
    ctx, calls, _ = _gateway(monkeypatch, _daily)
    out = await deep.call_user_tool(ctx, "gw", "funnel-analytics__get_daily_sales", {"brand": "one8", "date": "2026-11-24"})
    assert [c["arguments"] for c in calls if "tool_name" in c][-1] == {"brand": "one8", "start": "2026-11-24", "end": "2026-11-24"}
    assert out["window"] == {"start": "2026-11-24", "end": "2026-11-24"}


async def test_any_filter_the_source_ignored_is_renamed_not_just_dates(monkeypatch):
    def answer(inner):
        return {"brand": inner.get("brand", "all"), "channel": inner.get("channel", "all"), "rows": [{"sales": 5}]}

    ctx, calls, _ = _gateway(monkeypatch, answer)
    out = await deep.call_user_tool(ctx, "gw", "funnel-analytics__get_channel_sales", {"brand_name": "one8", "sales_channel": "Meta"})
    assert [c["arguments"] for c in calls if "tool_name" in c][-1] == {"brand": "one8", "channel": "Meta"}
    assert out["brand"] == "one8" and out["channel"] == "Meta"


async def test_nothing_to_rename_to_tells_the_agent_the_names_the_answer_uses(monkeypatch):
    def answer(inner):
        return {"window": {"start": "2026-10-26", "end": "2026-11-24"}, "rows": []}  # ignores everything, always

    ctx, calls, store = _gateway(monkeypatch, answer)
    out = await deep.call_user_tool(ctx, "gw", "funnel-analytics__get_daily_sales", {"from_date": "2026-11-24", "to_date": "2026-11-24"})
    assert out["args_ignored"] and out["arguments_expected"] == ["start", "end"] and "start, end" in out["note"]
    assert not store  # a rename that didn't help isn't kept


def test_a_filter_the_answer_agrees_with_is_not_flagged():
    from app.agent import arg_repair

    assert arg_repair.ignored_filters({"brand": "One8"}, {"brand": "one8"}) == []  # same filter, case aside
    assert arg_repair.ignored_filters({"brand_id": "7"}, {"store_id": "3"}) == []  # different things that share "id"
    assert arg_repair.ignored_filters({"limit": 5}, {"rows": []}) == []
    assert arg_repair.ignored_filters({"category": "Shoes"}, {"category_count": 9}) == []  # a count, not the filter
    assert arg_repair.ignored_filters({"brand_name": "One8"}, {"brand": "all"}) == [("brand_name", "brand")]
    assert arg_repair.ignored_filters({"query": "shoes"}, {"query_id": "abc"}) == []  # a different thing, not the filter


async def test_a_call_that_may_change_data_is_never_sent_twice(monkeypatch):
    ctx, calls, store = _gateway(monkeypatch, _daily)
    monkeypatch.setattr(deep, "_plain_read", lambda tool_name, args, hit: False)  # e.g. create_report: not a plain read
    from app.agent import approval

    monkeypatch.setattr(approval, "gateway_needs_approval", lambda *a, **k: False)  # let it run without a card
    store[("u1", "gw", "funnel-analytics__create_daily_report")] = {"from_date": "start"}
    await deep.call_user_tool(ctx, "gw", "funnel-analytics__create_daily_report", {"brand": "one8", "from_date": "2026-11-24", "to_date": "2026-11-24"})
    runs = [c["arguments"] for c in calls if "tool_name" in c]
    assert runs == [{"brand": "one8", "from_date": "2026-11-24", "to_date": "2026-11-24"}]  # once, exactly as approved


async def test_a_filter_that_only_looks_different_is_not_marked_wrong(monkeypatch):
    def answer(inner):
        return {"brand": "One 8 Commune", "rows": [{"sales": 5}]}  # the source's display name for what was asked

    ctx, calls, store = _gateway(monkeypatch, answer)
    out = await deep.call_user_tool(ctx, "gw", "funnel-analytics__get_brand_sales", {"brand_name": "one8"})
    assert not out.get("args_ignored") and "note" not in out and not store


async def test_names_kept_from_before_that_stop_working_are_dropped(monkeypatch):
    ctx, calls, store = _gateway(monkeypatch, _daily)
    from app.agent import arg_repair

    async def forget(user_id, slug, action):
        store.pop((user_id, slug, action), None)

    monkeypatch.setattr(arg_repair, "forget", forget)
    store[("u1", "gw", "funnel-analytics__get_daily_sales")] = {"start": "since"}  # stale: the source takes start now
    await deep.call_user_tool(ctx, "gw", "funnel-analytics__get_daily_sales", {"brand": "one8", "start": "2026-11-24", "end": "2026-11-24"})
    assert ("u1", "gw", "funnel-analytics__get_daily_sales") not in store or store[("u1", "gw", "funnel-analytics__get_daily_sales")] != {"start": "since"}
