import json
from pathlib import Path
from types import SimpleNamespace

from app.agent.deep import (
    RunContext,
    call_user_tool,
    compact_payload,
    harness_tools,
    inspect_user_result,
    last_text,
    list_user_connectors,
    system_prompt,
)


def _conn(**kwargs):
    defaults = {
        "id": "1",
        "name": "mcp-gateway",
        "alias": "digital",
        "slug": "digital",
        "kind": "mcp",
        "provider": "mcp",
        "description": "Retail KPI gateway",
        "server_key": "mcp:digital",
        "health": "ok",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _ctx(connectors, **kwargs):
    user = SimpleNamespace(id="u1", timezone="Asia/Kolkata")
    return RunContext(db=None, user=user, connectors={c.slug: c for c in connectors}, **kwargs)


async def test_list_connectors_is_this_user_only():
    mine = _conn()
    ctx = _ctx([mine])
    listed = await list_user_connectors(ctx)
    assert [item["slug"] for item in listed] == ["digital"]
    assert "gmail" not in json.dumps(listed)


async def test_unknown_slug_is_rejected():
    ctx = _ctx([_conn()])
    result = await call_user_tool(ctx, "gmail", "list_messages", {})
    assert result["error"]
    assert "gmail" in result["error"]
    assert ctx.used_tools == []


async def test_multi_call_records_each_execute_source():
    calls = []

    async def fake_call(slug, tool, args):
        calls.append((slug, tool, args))
        window = (args.get("arguments") or args).get("start") or ""
        return {"kpis": {"net_sales": {"value": 10 if window.startswith("2026-07") else 20}}}

    ctx = _ctx([_conn()], call_impl=fake_call)
    first = await call_user_tool(
        ctx,
        "digital",
        "gateway__execute",
        {"tool_name": "get_sales_dashboard", "arguments": {"brand": "acme", "start": "2026-07-01", "end": "2026-07-31"}},
    )
    second = await call_user_tool(
        ctx,
        "digital",
        "gateway__execute",
        {"tool_name": "get_sales_dashboard", "arguments": {"brand": "acme", "start": "2026-08-01", "end": "2026-08-31"}},
    )
    assert first["kpis"]["net_sales"]["value"] == 10
    assert second["kpis"]["net_sales"]["value"] == 20
    assert len(calls) == 2
    assert ctx.used_tools.count("digital__gateway__execute") == 2
    assert ctx.sources[-1]["source_tool"] == "digital__get_sales_dashboard"
    assert "get_sales_dashboard" in json.dumps(ctx.sources)


async def test_catalog_search_is_not_stored_as_source():
    async def fake_call(slug, tool, args):
        return {"candidates": [{"public_name": "any_tool"}]}

    ctx = _ctx([_conn()], call_impl=fake_call)
    await call_user_tool(ctx, "digital", "gateway__tool_search", {"query": "sales"})
    assert ctx.used_tools == ["digital__gateway__tool_search"]
    assert ctx.sources == []


def test_compact_payload_keeps_a_map_not_a_chopped_string():
    huge = {"kpis": {"net_sales": 10}, "rows": [{"n": i, "label": f"row-{i}"} for i in range(5000)]}
    out = compact_payload(huge, 200)
    assert out["too_large"] is True
    assert out["map"]["rows"]["count"] == 5000
    assert "n" in out["map"]["rows"]["item_keys"]
    assert "preview" not in out


async def test_inspect_pages_the_asked_breakdown():
    rows = [{"style": f"S{i}", "net_sales": i} for i in range(250)]

    async def fake_call(slug, tool, args):
        return {"kpis": {"net_sales": {"value": 99}}, "breakdowns": {"by_style": rows}}

    ctx = _ctx([_conn()], call_impl=fake_call)
    first = await call_user_tool(ctx, "digital", "gateway__execute", {"tool_name": "get_sales_dashboard", "arguments": {}})
    assert ctx.results
    found = inspect_user_result(ctx, search="style", offset=0, limit=80)
    assert found["path"]
    assert found["data"]["total"] == 250
    assert found["data"]["items"][0]["style"] == "S0"
    assert found["data"]["next_offset"] == 80
    more = inspect_user_result(ctx, path=found["path"], offset=80, limit=80)
    assert more["data"]["items"][0]["style"] == "S80"
    if isinstance(first, dict) and first.get("too_large"):
        assert first.get("result_id")


def test_system_prompt_has_no_product_recipes():
    prompt = system_prompt(
        SimpleNamespace(timezone="Asia/Kolkata"),
        [_conn()],
        "",
    )
    lowered = prompt.lower()
    assert "list_connectors" in lowered
    assert "call_tool" in lowered
    assert "inspect_result" in lowered
    assert "schedule_task" in lowered
    assert "do not fetch reports or send mail now" in lowered
    assert "one8" not in lowered
    assert "lotto" not in lowered
    assert "grain_windows" not in lowered
    assert "@digital" in prompt


def test_agent_package_has_no_brand_date_heuristics():
    root = Path(__file__).resolve().parents[1] / "app" / "agent"
    banned = ("one8", "lotto", "grain_windows", "entity_slug", "month on month", "prefer_planned")
    for path in root.glob("*.py"):
        text = path.read_text().lower()
        for token in banned:
            assert token not in text, f"{path.name} contains {token}"


def test_harness_exposes_schedule_task():
    ctx = _ctx([_conn()])
    names = [tool.name for tool in harness_tools(ctx)]
    assert "schedule_task" in names
    schedule = next(tool for tool in harness_tools(ctx) if tool.name == "schedule_task")
    assert "future" in (schedule.description or "").lower()


def test_last_text_skips_tool_calls():
    ai = SimpleNamespace(type="ai", tool_calls=[{"name": "call_tool"}], content="")
    final = SimpleNamespace(type="ai", tool_calls=[], content="Net sales were 12.")
    assert last_text([ai, final]) == "Net sales were 12."
