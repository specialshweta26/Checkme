import json
from pathlib import Path
from types import SimpleNamespace

from app.agent.deep import (
    RunContext,
    call_user_tool,
    compact_payload,
    dumps,
    dumps_full,
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


def test_catalog_dumps_are_not_truncated():
    listed = [
        {"slug": "gmail", "core_tools": [f"mail_tool_{i}" for i in range(80)]},
        {"slug": "outlook", "core_tools": [f"outlook_tool_{i}" for i in range(80)]},
        {"slug": "digital", "core_tools": [f"kpi_{i}" for i in range(80)], "kind": "mcp"},
    ]
    raw = dumps_full(listed)
    data = json.loads(raw)
    assert isinstance(data, list)
    assert [row["slug"] for row in data] == ["gmail", "outlook", "digital"]
    assert "too_large" not in raw
    assert len(data[2]["core_tools"]) == 80
    compact = json.loads(dumps(listed, 2000))
    assert isinstance(compact, dict) and compact.get("too_large") is True


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
    assert "capability phrase" in lowered
    assert "gateway__tool_search return catalogs" in lowered
    assert "call_tool" in lowered
    assert "inspect_result" in lowered
    assert "schedule_task" in lowered
    assert "create_event" in lowered
    assert "create_google_meet" in lowered
    assert "create_teams_meeting" in lowered
    assert "empty_folder" in lowered
    assert "get_thread" in lowered
    assert "never call get_thread with an empty thread_id" in lowered
    assert "detailed analyst" in lowered
    assert "keep answers short" not in lowered
    assert "schedule_task and do not fetch now" in lowered
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


async def test_open_mail_hydrates_full_thread():
    calls = []

    async def fake_call(slug, tool, args):
        calls.append((tool, dict(args)))
        if tool == "search_messages":
            return {"messages": [{"message_id": "AAMk1", "thread_id": "", "subject": "Re: Adidas"}]}
        if tool == "get_thread":
            return {
                "thread_id": "AAQkConv",
                "subject": "Adidas",
                "message_count": 2,
                "last_message_id": "AAMk1",
                "messages": [
                    {"message_id": "m0", "from": "Regina", "body": "Please find the profiles."},
                    {"message_id": "AAMk1", "from": "Gourishanker", "body": "Please take this forward."},
                ],
            }
        return {}

    conn = _conn(slug="outlook", provider="outlook", name="Outlook", alias="outlook")
    ctx = _ctx([conn], call_impl=fake_call, query="open this mail thread Adidas Data Engineer")
    result = await call_user_tool(ctx, "outlook", "search_messages", {"query": "Adidas Data Engineer"})
    assert result["opened_thread"]["message_count"] == 2
    assert result["thread_id"] == "AAQkConv"
    assert result["message_id"] == "AAMk1"
    assert any(tool == "get_thread" and args.get("message_id") == "AAMk1" for tool, args in calls)
    assert "outlook__get_thread" in ctx.used_tools


async def test_get_thread_fills_empty_id_from_prior_search():
    async def fake_call(slug, tool, args):
        if tool == "search_messages":
            return {"messages": [{"message_id": "m1", "thread_id": "t1", "subject": "Hi"}]}
        if tool == "get_thread":
            return {
                "thread_id": args.get("thread_id") or "t1",
                "message_count": 1,
                "last_message_id": args.get("message_id") or "m1",
                "messages": [{"message_id": "m1", "body": "hi"}],
            }
        return {}

    conn = _conn(slug="outlook", provider="outlook", name="Outlook", alias="outlook")
    ctx = _ctx([conn], call_impl=fake_call, query="find Adidas profiles")
    await call_user_tool(ctx, "outlook", "search_messages", {"query": "Adidas profiles"})
    out = await call_user_tool(ctx, "outlook", "get_thread", {"thread_id": ""})
    assert out["thread_id"] == "t1"
    assert out["last_message_id"] == "m1"
