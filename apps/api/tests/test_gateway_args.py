"""Gateway actions are called with their own argument names (docs/LLD.md "Gateway arguments"): a gateway ignores names it
doesn't know and answers for its defaults, which looked like zero sales."""

from __future__ import annotations

from types import SimpleNamespace

from app.agent import deep
from app.connectors.shape import slim_search
from app.routing.retrieve import nested_tool_rows

SEARCH = {"candidates": [{"public_name": "get_daily_sales", "description": "Day-level sales",
                          "inputSchema": {"type": "object", "properties": {"brand_id": {}, "since": {}, "until": {}}, "required": ["brand_id"]}}]}  # fmt: skip


def test_search_results_keep_each_actions_arguments_for_the_agent():
    slim = slim_search(SEARCH, "daily sales")
    assert slim["candidates"][0]["arguments"] == ["brand_id", "since", "until"] and slim["candidates"][0]["required"] == ["brand_id"]
    rows = nested_tool_rows("agilitas", "Agilitas", slim["candidates"], "execute", ["sales"])
    assert rows[0]["arguments"] == ["brand_id", "since", "until"] and rows[0]["required"] == ["brand_id"]


def test_wrong_or_missing_argument_names_are_caught_before_the_call():
    known = {"fields": ["brand_id", "since", "until"], "required": ["brand_id"]}
    wrong = deep.check_gateway_args(known, "get_daily_sales", {"brand": "one8", "start": "2026-11-01", "end": "2026-11-16"})
    assert wrong["not_run"] and "get_daily_sales takes brand_id, since, until" in wrong["error"]
    assert "you sent brand, start, end" in wrong["error"] and "it needs brand_id" in wrong["error"]
    assert deep.check_gateway_args(known, "get_daily_sales", {"brand_id": "one8", "since": "2026-11-01"}) is None
    assert deep.check_gateway_args(None, "x", {"anything": 1}) is None  # parameters unknown: never block


def _gateway(calls, search=SEARCH):
    async def fake_call(slug, tool, args):
        calls.append((tool, dict(args)))
        if tool == "gateway__tool_search":
            return search
        return {"rows": [{"date": "2026-11-01", "net_sales": 41200}]}

    conn = SimpleNamespace(slug="agilitas", name="Agilitas", alias="", kind="mcp", provider="mcp", server_url="https://gw.test/mcp",
                           server_key="mcp:gw", id="c1", health="ok")  # fmt: skip
    ctx = deep.RunContext(db=None, user=SimpleNamespace(id="u1"), connectors={"agilitas": conn}, auto_confirm=True, call_impl=fake_call)
    ctx.gateway_surface["agilitas"] = {"search": "gateway__tool_search", "search_schema": {"type": "object", "properties": {"query": {}}},
                                       "execute": "gateway__execute", "resolve": None, "resolve_schema": {}}  # fmt: skip
    return ctx


async def test_the_agents_call_is_corrected_not_sent_with_guessed_names():
    calls = []
    ctx = _gateway(calls)
    out = await deep.call_user_tool(ctx, "agilitas", "get_daily_sales", {"brand": "one8", "start": "2026-11-01", "end": "2026-11-16"})
    assert out["not_run"] and out["arguments_expected"] == ["brand_id", "since", "until"]
    assert not any(tool == "gateway__execute" for tool, _ in calls)
    out = await deep.call_user_tool(ctx, "agilitas", "get_daily_sales", {"brand_id": "one8", "since": "2026-11-01", "until": "2026-11-16"})
    assert out["rows"][0]["net_sales"] == 41200
    sent = [args for tool, args in calls if tool == "gateway__execute"]
    assert sent == [{"tool_name": "get_daily_sales", "arguments": {"brand_id": "one8", "since": "2026-11-01", "until": "2026-11-16"}}]
    assert sum(1 for tool, _ in calls if tool == "gateway__tool_search") == 1  # looked up once per turn


async def test_the_schema_lookup_names_the_real_arguments():
    ctx = _gateway([])
    schema = await deep.get_user_tool_schema(ctx, "agilitas", "get_daily_sales")
    assert list(schema["parameters"]["properties"]) == ["brand_id", "since", "until"] and schema["parameters"]["required"] == ["brand_id"]


async def test_a_gateway_that_does_not_describe_parameters_is_never_blocked():
    calls = []
    ctx = _gateway(calls, search={"candidates": [{"public_name": "get_daily_sales", "description": "x"}]})
    out = await deep.call_user_tool(ctx, "agilitas", "get_daily_sales", {"brand": "one8"})
    assert out["rows"] and any(tool == "gateway__execute" for tool, _ in calls)
