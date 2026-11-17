"""A connected MCP's own tool is called exactly as its schema says (docs/LLD.md "Calling a tool by its schema"): a tool
ignores names it doesn't know and answers for its defaults, so guessed names gave another period and no data."""

from __future__ import annotations

from types import SimpleNamespace

from app.agent import deep

SCHEMA = {"type": "object", "properties": {"brand": {"type": "string", "enum": ["one8", "lotto", "hoka"]},
                                           "start": {"type": "string", "format": "date"}, "end": {"type": "string", "format": "date"}},
          "required": ["brand"]}  # fmt: skip


def test_guessed_names_are_not_sent_and_the_real_parameters_come_back():
    args, wrong = deep.check_schema_args(SCHEMA, "get_catalog_performance", {"brand": "Lotto", "start_date": "2026-11-16", "end_date": "2026-11-17"})
    assert wrong["not_run"] and "takes brand, start, end" in wrong["error"] and "start_date, end_date" in wrong["error"]
    assert wrong["parameters"]["brand"]["enum"] == ["one8", "lotto", "hoka"]


def test_case_of_names_and_allowed_values_is_corrected():
    args, wrong = deep.check_schema_args(SCHEMA, "t", {"Brand": "Lotto", "START": "2026-11-16", "end": "2026-11-17"})
    assert wrong is None and args == {"brand": "lotto", "start": "2026-11-16", "end": "2026-11-17"}


def test_missing_required_is_caught_and_open_schemas_are_never_blocked():
    assert deep.check_schema_args(SCHEMA, "t", {"start": "2026-11-16"})[1]["not_run"]
    open_schema = {**SCHEMA, "additionalProperties": True}
    assert deep.check_schema_args(open_schema, "t", {"brand": "lotto", "extra": 1}) == ({"brand": "lotto", "extra": 1}, None)
    assert deep.check_schema_args({"type": "object"}, "t", {"anything": 1}) == ({"anything": 1}, None)
    assert deep.check_schema_args(None, "t", {"x": 1}) == ({"x": 1}, None)


def test_enum_inside_any_of_and_arrays():
    schema = {"type": "object", "properties": {"brand": {"anyOf": [{"enum": ["one8", "lotto"]}, {"type": "null"}]},
                                               "brands": {"type": "array", "items": {"enum": ["one8", "lotto"]}}}}  # fmt: skip
    args, wrong = deep.check_schema_args(schema, "t", {"brand": "LOTTO", "brands": ["One8", "Lotto"]})
    assert wrong is None and args == {"brand": "lotto", "brands": ["one8", "lotto"]}


async def test_the_agents_call_to_a_connected_tool_is_checked(monkeypatch):
    sent = []

    async def invoke(db, user, conn, tool_name, args, attachments=None):
        sent.append(args)
        return {"rows": [{"sku": "A", "units": 4}]}

    hit = SimpleNamespace(tool_name="get_catalog_performance", schema=SCHEMA, read_only=True, destructive=False, connector_name="Gateway")

    async def load(db, conns, wanted):
        return [hit]

    monkeypatch.setattr(deep, "invoke_named", invoke)
    monkeypatch.setattr(deep, "load_named", load)
    conn = SimpleNamespace(kind="mcp", server_url="https://mcp.example.test/mcp", alias="", name="Gateway", server_key="mcp:gw",
                           id="c1", health="ok", slug="gw")  # fmt: skip
    ctx = deep.RunContext(db=object(), user=SimpleNamespace(id="u1"), connectors={"gw": conn}, auto_confirm=True)
    monkeypatch.setattr(deep, "bump_usage", lambda *a, **k: _none())
    out = await deep.call_user_tool(ctx, "gw", "get_catalog_performance", {"brand": "Lotto", "start_date": "2026-11-16"})
    assert out["not_run"] and sent == []
    out = await deep.call_user_tool(ctx, "gw", "get_catalog_performance", {"brand": "Lotto", "start": "2026-11-16", "end": "2026-11-17"})
    assert sent == [{"brand": "lotto", "start": "2026-11-16", "end": "2026-11-17"}]


async def _none():
    return None


# --- the schema reaches the agent, and listed values are looked up -------------------------------------------------

LISTED = {"type": "object", "required": ["brand"], "additionalProperties": False,
          "properties": {"brand": {"type": "string", "description": "Brand slug (see list_brands)."},
                         "start": {"type": "string", "description": "ISO date YYYY-MM-DD."},
                         "end": {"type": "string", "description": "ISO date YYYY-MM-DD."}}}  # fmt: skip


def test_the_brief_schema_carries_what_the_agent_needs():
    brief = deep.brief_schema({**SCHEMA, "additionalProperties": False})
    assert brief["properties"]["brand"]["allowed"] == ["one8", "lotto", "hoka"] and brief["required"] == ["brand"]
    assert brief["properties"]["start"]["format"] == "date" and brief["only_these_names"]
    assert deep.brief_schema({"type": "object", "properties": {}}) is None


def _hit(name, schema, read_only=True):
    return SimpleNamespace(tool_name=name, schema=schema, read_only=read_only, destructive=not read_only, connector_name="Gateway",
                           slug="gw", description="", group="", produces=[])  # fmt: skip


def _ctx_with(monkeypatch, hits, answers):
    sent = []

    async def invoke(db, user, conn, tool_name, args, attachments=None):
        sent.append((tool_name, dict(args)))
        return answers[tool_name]

    async def load(db, conns, wanted):
        return [h for h in hits if ("gw", h.tool_name) in wanted]

    async def no_usage(*a, **k):
        return None

    monkeypatch.setattr(deep, "invoke_named", invoke)
    monkeypatch.setattr(deep, "load_named", load)
    monkeypatch.setattr(deep, "bump_usage", no_usage)
    conn = SimpleNamespace(kind="mcp", server_url="https://mcp.example.test/mcp", alias="", name="Gateway", server_key="mcp:gw",
                           id="c1", health="ok", slug="gw")  # fmt: skip
    return deep.RunContext(db=object(), user=SimpleNamespace(id="u1"), connectors={"gw": conn}, auto_confirm=True), sent


async def test_a_value_the_schema_says_comes_from_another_tool_is_looked_up(monkeypatch):
    hits = [_hit("srv__get_catalog_performance", LISTED), _hit("srv__list_brands", {"type": "object", "properties": {}})]
    answers = {"srv__list_brands": {"brands": [{"slug": "one8", "name": "One8"}, {"slug": "lotto", "name": "Lotto"}]},
               "srv__get_catalog_performance": {"rows": [{"sku": "A", "units": 4}]}}  # fmt: skip
    ctx, sent = _ctx_with(monkeypatch, hits, answers)
    out = await deep.call_user_tool(ctx, "gw", "srv__get_catalog_performance", {"brand": "Lotto", "start": "2026-11-16", "end": "2026-11-17"})
    assert out["rows"] and sent[-1] == ("srv__get_catalog_performance", {"brand": "lotto", "start": "2026-11-16", "end": "2026-11-17"})
    await deep.call_user_tool(ctx, "gw", "srv__get_catalog_performance", {"brand": "One8"})
    assert [t for t, _ in sent].count("srv__list_brands") == 1  # looked up once per turn


async def test_a_value_the_listing_does_not_have_is_not_sent(monkeypatch):
    hits = [_hit("srv__get_catalog_performance", LISTED), _hit("srv__list_brands", {"type": "object", "properties": {}})]
    answers = {"srv__list_brands": {"brands": [{"slug": "one8", "name": "One8"}]}, "srv__get_catalog_performance": {}}
    ctx, sent = _ctx_with(monkeypatch, hits, answers)
    out = await deep.call_user_tool(ctx, "gw", "srv__get_catalog_performance", {"brand": "Nike"})
    assert out["not_run"] and "listed_values" in out and all(t != "srv__get_catalog_performance" for t, _ in sent)


async def test_a_listing_tool_that_writes_or_needs_arguments_is_not_called(monkeypatch):
    hits = [_hit("srv__get_catalog_performance", LISTED),
            _hit("srv__list_brands", {"type": "object", "properties": {"q": {}}, "required": ["q"]})]  # fmt: skip
    answers = {"srv__get_catalog_performance": {"rows": [1]}}
    ctx, sent = _ctx_with(monkeypatch, hits, answers)
    await deep.call_user_tool(ctx, "gw", "srv__get_catalog_performance", {"brand": "lotto"})
    assert [t for t, _ in sent] == ["srv__get_catalog_performance"]


async def test_search_results_carry_each_tools_schema(monkeypatch):
    async def found(db, conns, query, limit):
        return [_hit("srv__get_catalog_performance", LISTED)]

    async def no_catalog(ctx, conn, query):
        return []

    monkeypatch.setattr(deep, "search_tools", found)
    monkeypatch.setattr(deep, "_expand_gateway_catalog", no_catalog)
    conn = SimpleNamespace(kind="mcp", server_url="https://mcp.example.test/mcp", alias="", name="Gateway", server_key="mcp:gw",
                           id="c1", health="ok", slug="gw", provider="mcp")  # fmt: skip
    ctx = deep.RunContext(db=object(), user=SimpleNamespace(id="u1"), connectors={"gw": conn})
    rows = await deep.search_user_tools(ctx, "Lotto product sales 16-17 Nov")
    params = rows[0]["parameters"]
    assert set(params["properties"]) == {"brand", "start", "end"} and params["only_these_names"]
    assert "list_brands" in params["properties"]["brand"]["description"]


# --- slugs, listings and gateway actions -----------------------------------------------------------------------------


def test_a_parameter_the_schema_calls_a_slug_gets_a_slug():
    args, wrong = deep.check_schema_args(LISTED, "t", {"brand": "Lotto", "start": "2026-11-09", "end": "2026-11-17"})
    assert wrong is None and args["brand"] == "lotto"
    multi = {"type": "object", "properties": {"store_slug": {"type": "string"}}}
    assert deep.check_schema_args(multi, "t", {"store_slug": "Bandra West"})[0] == {"store_slug": "bandra-west"}


def test_a_listing_row_gives_its_slug_not_the_display_name():
    rows = {"brands": [{"brand": "Lotto", "slug": "lotto"}, {"brand": "One 8", "slug": "one8"}]}
    assert deep._value_from_listing("Lotto", rows, "brand") == "lotto"
    assert deep._value_from_listing("one 8", rows, "brand") == "one8"


async def test_a_gateway_action_is_checked_against_its_catalog_schema(monkeypatch):
    sent = []

    async def fake_call(slug, tool, args):
        sent.append((tool, dict(args)))
        if tool == "gateway__tool_search":
            return {"candidates": [{"public_name": "srv__get_daily_sales", "description": "Day-level sales", "inputSchema": LISTED}]}
        return {"days": [{"net_sales": 640978}]}

    conn = SimpleNamespace(slug="gw", name="Gateway", alias="", kind="mcp", provider="mcp", server_url="https://gw.test/mcp",
                           server_key="mcp:gw", id="c1", health="ok")  # fmt: skip
    ctx = deep.RunContext(db=None, user=SimpleNamespace(id="u1"), connectors={"gw": conn}, auto_confirm=True, call_impl=fake_call)
    ctx.gateway_surface["gw"] = {"search": "gateway__tool_search", "search_schema": {"type": "object", "properties": {"query": {}}},
                                 "execute": "gateway__execute", "execute_schema": {}, "resolve": None, "resolve_schema": {}}  # fmt: skip
    wrong = await deep.call_user_tool(ctx, "gw", "srv__get_daily_sales", {"brand": "Lotto", "start_date": "2026-11-09"})
    assert wrong["not_run"] and "start_date" in wrong["error"]
    out = await deep.call_user_tool(ctx, "gw", "srv__get_daily_sales", {"brand": "Lotto", "start": "2026-11-09", "end": "2026-11-17"})
    assert out["days"][0]["net_sales"] == 640978
    assert sent[-1] == ("gateway__execute", {"tool_name": "srv__get_daily_sales",
                                             "arguments": {"brand": "lotto", "start": "2026-11-09", "end": "2026-11-17"}})  # fmt: skip


# --- values that were rewritten into ones the tool doesn't know (empty answers) ---------------------------------------


def test_a_slug_keeps_its_underscores_and_ids_are_not_hyphenated():
    schema = {"type": "object", "properties": {"brand_slug": {"type": "string", "description": "Brand slug (see list_brands)"},
                                               "store_id": {"type": "string", "description": "Store id, the slug of the store"},
                                               "code": {"type": "string"}}}  # fmt: skip
    args, wrong = deep.check_schema_args(schema, "t", {"brand_slug": "nike_in", "store_id": "blr_01", "code": "AB_12"})
    assert wrong is None and args == {"brand_slug": "nike_in", "store_id": "blr_01", "code": "AB_12"}
    assert deep.check_schema_args(schema, "t", {"brand_slug": "Nike_IN"})[0] == {"brand_slug": "nike_in"}
    assert deep.check_schema_args(schema, "t", {"brand_slug": "Nike India"})[0] == {"brand_slug": "nike-india"}


def test_only_tool_names_count_as_where_values_come_from():
    for text in ("Data from the warehouse", "Use the brand code", "Start date, from which to report", "via email"):
        assert deep._SEE_TOOL.search(text) is None, text
    for text, tool in (("Brand slug (see list_brands)", "list_brands"), ("returned by `get_stores()`", "get_stores")):
        assert deep._SEE_TOOL.search(text).group(1) == tool


async def test_a_value_in_a_text_listing_is_sent_not_refused(monkeypatch):
    hits = [_hit("srv__get_catalog_performance", LISTED), _hit("srv__list_brands", {"type": "object", "properties": {}})]
    answers = {"srv__list_brands": {"text": "| slug | name |\n|---|---|\n| one8 | One 8 |\n| lotto_in | Lotto India |"},
               "srv__get_catalog_performance": {"rows": [{"sku": "A", "units": 4}]}}  # fmt: skip
    ctx, sent = _ctx_with(monkeypatch, hits, answers)
    out = await deep.call_user_tool(ctx, "gw", "srv__get_catalog_performance", {"brand": "lotto_in"})
    assert out["rows"] and sent[-1] == ("srv__get_catalog_performance", {"brand": "lotto_in"})
    # A listing the lookup can't read (prose) never blocks the call.
    out = await deep.call_user_tool(ctx, "gw", "srv__get_catalog_performance", {"brand": "Puma"})
    assert out["rows"] and sent[-1] == ("srv__get_catalog_performance", {"brand": "puma"})
