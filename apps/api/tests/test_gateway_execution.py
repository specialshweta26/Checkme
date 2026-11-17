"""Calls reach a gateway the way other MCP clients send them (docs/LLD.md "Gateway execution"): the connector's own
listed tool is called directly, and an execute call is shaped by execute's own input schema."""

from __future__ import annotations

from types import SimpleNamespace

from app.agent import deep
from app.routing import select as routing_select

SCHEMA = {"type": "object", "required": ["brand"], "additionalProperties": False,
          "properties": {"brand": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"}}}  # fmt: skip


def _hit(name, schema=SCHEMA):
    return SimpleNamespace(tool_name=name, schema=schema, read_only=True, destructive=False, connector_name="Gateway", slug="gw",
                           description="", group="", produces=[])  # fmt: skip


def _setup(monkeypatch, listed):
    sent = []

    async def invoke(db, user, conn, tool_name, args, attachments=None):
        sent.append((tool_name, dict(args)))
        return {"rows": [{"sku": "A", "units": 4, "net_sales": 9800}]}

    async def load(db, conns, wanted):
        return [_hit(n) for (_, n) in wanted if n in listed]

    async def by_suffix(db, conn, name):
        found = [n for n in listed if n.endswith("__" + name)]
        return _hit(found[0]) if len(found) == 1 else None

    async def no_usage(*a, **k):
        return None

    monkeypatch.setattr(deep, "invoke_named", invoke)
    monkeypatch.setattr(deep, "load_named", load)
    monkeypatch.setattr(routing_select, "load_listed_as", by_suffix)
    monkeypatch.setattr(deep, "bump_usage", no_usage)
    conn = SimpleNamespace(kind="mcp", server_url="https://gw.example.test/mcp", alias="", name="Gateway", server_key="mcp:gw",
                           id="c1", health="ok", slug="gw", provider="mcp")  # fmt: skip
    ctx = deep.RunContext(db=object(), user=SimpleNamespace(id="u1"), connectors={"gw": conn}, auto_confirm=True, query="sales 16-17 Nov")
    return ctx, sent


async def test_a_name_without_its_server_prefix_calls_the_listed_tool_directly(monkeypatch):
    ctx, sent = _setup(monkeypatch, {"srv__get_daily_sales", "gateway__execute"})
    out = await deep.call_user_tool(ctx, "gw", "get_daily_sales", {"brand": "x", "start": "2026-11-16", "end": "2026-11-17"})
    assert out["rows"] and sent == [("srv__get_daily_sales", {"brand": "x", "start": "2026-11-16", "end": "2026-11-17"})]


async def test_an_execute_call_for_a_listed_tool_is_sent_directly(monkeypatch):
    ctx, sent = _setup(monkeypatch, {"srv__get_daily_sales", "gateway__execute"})
    await deep.call_user_tool(ctx, "gw", "gateway__execute",
                              {"tool_name": "get_daily_sales", "arguments": {"brand": "x", "start": "2026-11-16"}})  # fmt: skip
    assert sent == [("srv__get_daily_sales", {"brand": "x", "start": "2026-11-16"})]


def test_execute_is_shaped_by_its_own_schema():
    schema = {"type": "object", "required": ["tool", "args", "query"],
              "properties": {"tool": {"type": "string"}, "args": {"type": "object"}, "query": {"type": "string"}}}  # fmt: skip
    out = deep.execute_args(schema, {"tool_name": "get_daily_sales", "arguments": {"brand": "x"}}, "sales 16-17 Nov")
    assert out == {"tool": "get_daily_sales", "args": {"brand": "x"}, "query": "sales 16-17 Nov"}
    as_text = {"type": "object", "properties": {"public_tool_name": {"type": "string"}, "input": {"type": "string"}}}
    assert deep.execute_args(as_text, {"tool_name": "t", "arguments": {"a": 1}}) == {"public_tool_name": "t", "input": '{"a": 1}'}
    same = {"type": "object", "properties": {"tool_name": {}, "arguments": {}}}
    assert deep.execute_args(same, {"tool_name": "t", "arguments": {"a": 1}}) == {"tool_name": "t", "arguments": {"a": 1}}
    assert deep.execute_args({}, {"tool_name": "t", "arguments": {}}) == {"tool_name": "t", "arguments": {}}


# --- a display name for a value the server lists elsewhere (gateway traces: "Lotto" answered all zeros) --------------


def _gateway_without_schemas(monkeypatch):
    """A gateway reached only through search + execute, whose search describes actions without input schemas, and a
    sales action that matches the brand exactly (as the real one does: "Lotto" -> zeros, "lotto" -> figures)."""
    sent: list[tuple[str, dict]] = []
    searches: list[dict] = []
    catalog = [
        {"public_name": "funnel-analytics-prod__get_daily_sales", "description": "Daily sales.", "annotations": {"readOnlyHint": True}},
        {"public_name": "funnel-analytics-prod__list_brands", "description": "List the available brand slugs (e.g. one8, lotto).",
         "annotations": {"readOnlyHint": True}},
    ]  # fmt: skip

    async def surface(ctx, conn):
        return {"search": "gateway__tool_search", "execute": "gateway__execute", "search_schema": {},
                "execute_schema": {"type": "object", "properties": {"tool_name": {"type": "string"}, "arguments": {"type": "object"}}}}  # fmt: skip

    async def silent(ctx, conn, tool, args):
        searches.append(dict(args))
        return {"candidates": catalog}

    async def invoke(db, user, conn, tool_name, args, attachments=None):
        sent.append((tool_name, dict(args)))
        action, inner = args.get("tool_name"), args.get("arguments") or {}
        if action == "funnel-analytics-prod__list_brands":
            return {"brands": [{"slug": "one8", "name": "One8"}, {"slug": "lotto", "name": "Lotto"}]}
        figure = 660023 if inner.get("brand") in {"one8", "lotto"} else 0
        return {"brand": inner.get("brand"), "days": [{"date": "2026-11-10", "net_sales": figure}]}

    async def nothing_listed(db, conns, wanted):
        return []

    async def no_suffix(db, conn, name):
        return None

    async def no_usage(*a, **k):
        return None

    monkeypatch.setattr(deep, "_load_gateway_surface", surface)
    monkeypatch.setattr(deep, "_silent_call", silent)
    monkeypatch.setattr(deep, "invoke_named", invoke)
    monkeypatch.setattr(deep, "load_named", nothing_listed)
    monkeypatch.setattr(routing_select, "load_listed_as", no_suffix)
    monkeypatch.setattr(deep, "bump_usage", no_usage)
    conn = SimpleNamespace(kind="mcp", server_url="https://gw.example.test/mcp", alias="", name="Gateway", server_key="mcp:gw",
                           id="c1", health="ok", slug="gw", provider="mcp")  # fmt: skip
    ctx = deep.RunContext(db=object(), user=SimpleNamespace(id="u1"), connectors={"gw": conn}, auto_confirm=True,
                          query="Lotto sale in last 11 days")  # fmt: skip
    return ctx, sent, searches


async def test_a_display_name_is_sent_as_the_slug_the_server_lists(monkeypatch):
    ctx, sent, _ = _gateway_without_schemas(monkeypatch)
    out = await deep.call_user_tool(ctx, "gw", "funnel-analytics-prod__get_daily_sales",
                                    {"brand": "Lotto", "start": "2026-11-07", "end": "2026-11-17"})  # fmt: skip
    assert out["days"][0]["net_sales"] == 660023
    final = sent[-1][1]
    assert final["tool_name"] == "funnel-analytics-prod__get_daily_sales"
    assert final["arguments"] == {"brand": "lotto", "start": "2026-11-07", "end": "2026-11-17"}
    # The listing ran once; a second brand in the same turn reuses it.
    await deep.call_user_tool(ctx, "gw", "funnel-analytics-prod__get_daily_sales", {"brand": "One8"})
    assert sent[-1][1]["arguments"] == {"brand": "one8"}
    assert [a.get("tool_name") for _, a in sent].count("funnel-analytics-prod__list_brands") == 1


async def test_a_brand_the_server_does_not_list_is_sent_back_with_the_list(monkeypatch):
    ctx, sent, _ = _gateway_without_schemas(monkeypatch)
    out = await deep.call_user_tool(ctx, "gw", "funnel-analytics-prod__get_daily_sales", {"brand": "Puma"})
    assert out["not_run"] and "listed_values" in out
    assert all(a.get("tool_name") != "funnel-analytics-prod__get_daily_sales" for _, a in sent)


async def test_slugs_dates_and_free_text_are_not_looked_up(monkeypatch):
    ctx, sent, searches = _gateway_without_schemas(monkeypatch)
    await deep.call_user_tool(ctx, "gw", "funnel-analytics-prod__get_daily_sales",
                              {"brand": "lotto", "start": "2026-11-07", "trend": "wow"})  # fmt: skip
    assert [a.get("tool_name") for _, a in sent] == ["funnel-analytics-prod__get_daily_sales"]
    assert sent[-1][1]["arguments"] == {"brand": "lotto", "start": "2026-11-07", "trend": "wow"}


def test_where_a_value_is_listed_is_guessed_only_for_name_like_values():
    s = deep.sibling_listing_schema("srv__get_sales", {"brand": "Lotto", "store_id": "Bandra West", "start": "2026-11-07",
                                                        "gender": "men", "query": "Top Sellers", "limit": "Ten"})  # fmt: skip
    assert s == {"type": "object", "properties": {"brand": {"type": "string", "description": "(see list_brands)"},
                                                  "store_id": {"type": "string", "description": "(see list_stores)"}}}  # fmt: skip
    assert deep.sibling_listing_schema("get_sales", {"brand": "Lotto"}) is None  # no server prefix
    own = {"properties": {"brand": {"type": "string", "description": "Brand slug (see list_brands)."}}}
    assert deep.sibling_listing_schema("srv__get_sales", {"brand": "Lotto"}, own) is None  # its own schema already says
    assert deep.sibling_listing_schema("srv__get_sales", {"category": "Footwear"})["properties"]["category"]["description"] == "(see list_categories)"
