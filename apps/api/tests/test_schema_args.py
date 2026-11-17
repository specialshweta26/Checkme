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
