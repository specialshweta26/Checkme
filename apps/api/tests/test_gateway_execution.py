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


async def test_an_execute_call_made_by_the_agent_gets_the_same_lookup(monkeypatch):
    """The live trace: the agent called gateway__execute itself (a tool the connector lists), so the action's
    arguments skipped every check and "Lotto" went out."""
    ctx, sent, _ = _gateway_without_schemas(monkeypatch)

    async def execute_is_listed(db, conns, wanted):
        return [_hit(n, {"type": "object", "properties": {"tool_name": {"type": "string"}, "arguments": {"type": "object"}}})
                for (_, n) in wanted if n == "gateway__execute"]  # fmt: skip

    monkeypatch.setattr(deep, "load_named", execute_is_listed)
    out = await deep.call_user_tool(ctx, "gw", "gateway__execute",
                                    {"tool_name": "funnel-analytics-prod__get_daily_sales",
                                     "arguments": {"brand": "Lotto", "start": "2026-11-11", "end": "2026-11-17"}})  # fmt: skip
    assert out["days"][0]["net_sales"] == 660023
    assert sent[-1] == ("gateway__execute", {"tool_name": "funnel-analytics-prod__get_daily_sales",
                                             "arguments": {"brand": "lotto", "start": "2026-11-11", "end": "2026-11-17"}})  # fmt: skip
    # Arguments sent as a JSON string stay a string, with the slug inside.
    await deep.call_user_tool(ctx, "gw", "gateway__execute",
                              {"tool_name": "funnel-analytics-prod__get_daily_sales", "arguments": '{"brand": "One8"}'})  # fmt: skip
    import json as _json

    assert _json.loads(sent[-1][1]["arguments"]) == {"brand": "one8"}


async def test_an_action_named_with_execute_shaped_arguments_is_checked_inside(monkeypatch):
    """The live call: call_tool named funnel-analytics-prod__get_daily_sales but wrote its args as
    {"tool_name": ..., "arguments": {"brand": "Lotto", ...}}; those went out unchecked."""
    ctx, sent, _ = _gateway_without_schemas(monkeypatch)
    out = await deep.call_user_tool(ctx, "gw", "funnel-analytics-prod__get_daily_sales",
                                    {"tool_name": "funnel-analytics-prod__get_daily_sales",
                                     "arguments": {"brand": "Lotto", "start": "2026-11-13", "end": "2026-11-17"}})  # fmt: skip
    assert out["days"][0]["net_sales"] == 660023
    assert sent[-1] == ("gateway__execute", {"tool_name": "funnel-analytics-prod__get_daily_sales",
                                             "arguments": {"brand": "lotto", "start": "2026-11-13", "end": "2026-11-17"}})  # fmt: skip


# --- reads through execute run without asking; loose action names get the catalog's checks -----------------------

DASHBOARD_SCHEMA = {"type": "object", "required": ["brand"], "additionalProperties": False,
                    "properties": {"brand": {"type": "string", "description": "Brand slug (see list_brands)."},
                                   "start": {"type": "string", "description": "ISO date (optional)."},
                                   "end": {"type": "string", "description": "ISO date (optional)."}}}  # fmt: skip


def _gateway_with_catalog(monkeypatch, *, auto_confirm: bool):
    """The gateway as the live one: execute is listed and marked destructive, the catalog lists the real
    ``funnel-analytics-prod__…`` actions with their schemas, and the agent may name them loosely."""
    sent: list[tuple[str, dict]] = []
    catalog = [
        {"public_name": "funnel-analytics-prod__get_sales_dashboard", "description": "Core sales analytics.",
         "inputSchema": DASHBOARD_SCHEMA, "annotations": {"readOnlyHint": True}},
        {"public_name": "funnel-analytics-prod__list_brands", "description": "Brand slugs.", "annotations": {"readOnlyHint": True}},
        {"public_name": "erp-ag-prod__create_order", "description": "Create an order.",
         "inputSchema": {"type": "object", "properties": {"sku": {"type": "string"}}}},
    ]  # fmt: skip

    async def surface(ctx, conn):
        return {"search": "gateway__tool_search", "execute": "gateway__execute", "search_schema": {},
                "execute_schema": {"type": "object", "properties": {"tool_name": {"type": "string"}, "arguments": {"type": "object"}}}}  # fmt: skip

    async def silent(ctx, conn, tool, args):
        return {"candidates": catalog}

    async def invoke(db, user, conn, tool_name, args, attachments=None):
        sent.append((tool_name, dict(args)))
        if args.get("tool_name") == "funnel-analytics-prod__list_brands":
            return {"brands": [{"slug": "one8"}, {"slug": "lotto"}]}
        return {"ok": True, "window": (args.get("arguments") or {})}

    async def execute_listed(db, conns, wanted):
        return [SimpleNamespace(tool_name=n, schema={"type": "object", "properties": {"tool_name": {}, "arguments": {}}},
                                read_only=False, destructive=True, connector_name="Gateway", slug="gw", description="",
                                group="", produces=[])
                for (_, n) in wanted if n == "gateway__execute"]  # fmt: skip

    async def no_suffix(db, conn, name):
        return None

    async def no_usage(*a, **k):
        return None

    monkeypatch.setattr(deep, "_load_gateway_surface", surface)
    monkeypatch.setattr(deep, "_silent_call", silent)
    monkeypatch.setattr(deep, "invoke_named", invoke)
    monkeypatch.setattr(deep, "load_named", execute_listed)
    monkeypatch.setattr(routing_select, "load_listed_as", no_suffix)
    monkeypatch.setattr(deep, "bump_usage", no_usage)
    conn = SimpleNamespace(kind="mcp", server_url="https://gw.example.test/mcp", alias="", name="Gateway", server_key="mcp:gw",
                           id="c1", health="ok", slug="gw", provider="mcp")  # fmt: skip
    ctx = deep.RunContext(db=object(), user=SimpleNamespace(id="u1"), connectors={"gw": conn}, auto_confirm=auto_confirm,
                          query="One8 sales in the last 110 days")  # fmt: skip
    return ctx, sent


async def test_a_read_through_a_destructive_marked_execute_runs_without_asking(monkeypatch):
    ctx, sent = _gateway_with_catalog(monkeypatch, auto_confirm=False)
    out = await deep.call_user_tool(ctx, "gw", "gateway__execute",
                                    {"tool_name": "funnel-analytics-prod__get_sales_dashboard",
                                     "arguments": {"brand": "one8", "start": "2026-07-31", "end": "2026-11-17"}})  # fmt: skip
    assert out.get("ok") and not ctx.pending
    assert sent[-1][1]["arguments"] == {"brand": "one8", "start": "2026-07-31", "end": "2026-11-17"}
    # A write still asks.
    out = await deep.call_user_tool(ctx, "gw", "gateway__execute", {"tool_name": "erp-ag-prod__create_order", "arguments": {"sku": "A"}})
    assert out["status"] == "awaiting_user_confirmation" and ctx.pending
    assert all(a.get("tool_name") != "erp-ag-prod__create_order" for _, a in sent)


async def test_a_loose_action_name_gets_the_catalogs_checks_so_a_wrong_parameter_is_caught(monkeypatch):
    """The live 110-day run: funnel-analytics__get_sales_dashboard with {"days": 110} ran for its default 7 days."""
    ctx, sent = _gateway_with_catalog(monkeypatch, auto_confirm=False)
    out = await deep.call_user_tool(ctx, "gw", "gateway__execute",
                                    {"tool_name": "funnel-analytics__get_sales_dashboard", "arguments": {"brand": "One8", "days": 110}})  # fmt: skip
    assert out["not_run"] and "days" in out["error"] and set(out["arguments_expected"]) == {"brand", "start", "end"}
    assert not any((a.get("tool_name") or "").endswith("get_sales_dashboard") for _, a in sent) and not ctx.pending
    # Called by name too, then with the right parameters: it runs under the catalog's name, brand resolved.
    out = await deep.call_user_tool(ctx, "gw", "funnel-analytics__get_sales_dashboard",
                                    {"brand": "One8", "start": "2026-07-31", "end": "2026-11-17"})  # fmt: skip
    assert out.get("ok") and sent[-1] == ("gateway__execute", {"tool_name": "funnel-analytics-prod__get_sales_dashboard",
                                                              "arguments": {"brand": "one8", "start": "2026-07-31", "end": "2026-11-17"}})  # fmt: skip


async def test_canonical_action_leaves_known_and_ambiguous_names_alone(monkeypatch):
    ctx, _ = _gateway_with_catalog(monkeypatch, auto_confirm=True)
    assert await deep.canonical_action(ctx, ctx.connectors["gw"], "funnel-analytics-prod__get_sales_dashboard") == "funnel-analytics-prod__get_sales_dashboard"
    assert await deep.canonical_action(ctx, ctx.connectors["gw"], "funnel-analytics__get_sales_dashboard") == "funnel-analytics-prod__get_sales_dashboard"
    assert await deep.canonical_action(ctx, ctx.connectors["gw"], "other__get_nothing") == "other__get_nothing"


async def test_a_lumio_tool_sent_through_the_gateway_runs_where_it_lives(monkeypatch):
    """The live 110-day run: the report tool (the Computer connector's write_analytics_report) was sent through the
    gateway's execute and asked for approval as an unknown write."""
    ctx, sent = _gateway_with_catalog(monkeypatch, auto_confirm=False)
    computer = SimpleNamespace(kind="builtin", server_url="", alias="", name="Computer", server_key="builtin:computer", id="c2",
                               health="ok", slug="computer", provider="daytona")  # fmt: skip
    ctx.connectors["computer"] = computer

    async def listed(db, conns, wanted):
        out = []
        for slug, n in wanted:
            if slug == "gw" and n == "gateway__execute":
                out.append(SimpleNamespace(tool_name=n, schema={}, read_only=False, destructive=True, connector_name="Gateway",
                                           slug="gw", description="", group="", produces=[]))  # fmt: skip
            if slug == "computer" and n == "write_analytics_report":
                out.append(SimpleNamespace(tool_name=n, schema={}, read_only=False, destructive=False, connector_name="Computer",
                                           slug="computer", description="", group="", produces=[]))  # fmt: skip
        return out

    monkeypatch.setattr(deep, "load_named", listed)
    out = await deep.call_user_tool(ctx, "gw", "gateway__execute",
                                    {"tool_name": "write_analytics_report", "arguments": {"title": "Sales", "tables": []}})  # fmt: skip
    assert not ctx.pending and out.get("ok")
    assert sent[-1] == ("write_analytics_report", {"title": "Sales", "tables": []})  # on the Computer connector, not the gateway
    # A gateway action is still the gateway's.
    await deep.call_user_tool(ctx, "gw", "gateway__execute",
                              {"tool_name": "funnel-analytics-prod__get_sales_dashboard", "arguments": {"brand": "one8"}})  # fmt: skip
    assert sent[-1][0] == "gateway__execute"
