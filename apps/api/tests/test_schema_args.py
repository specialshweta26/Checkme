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
