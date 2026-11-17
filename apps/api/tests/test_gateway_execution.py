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
