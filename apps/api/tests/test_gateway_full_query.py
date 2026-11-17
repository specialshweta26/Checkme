"""A gateway's catalog search gets the whole request (docs/LLD.md "Gateway arguments"). A stub with the numbers cut out
matched a dashboard that answered zeros; the full request matched the daily sales action that has the data."""

from __future__ import annotations

from types import SimpleNamespace

from app.agent import deep

ASKED = "Get Lotto daily sales for today with net sales, orders and units"


def test_the_agents_own_words_are_sent_whole():
    out = deep._rewrite_catalog_search_args("gateway__tool_search", {"query": "Lotto daily sales last 6 days net sales orders units"}, ASKED)
    assert out == {"query": "Lotto daily sales last 6 days net sales orders units"}  # the 6 and the brand stay


def test_a_few_words_are_completed_with_the_users_question():
    out = deep._rewrite_catalog_search_args("gateway__tool_search", {"query": "sales"}, "One8 vs Lotto sales in last 6 days")
    assert "One8 vs Lotto sales in last 6 days" in out["query"]
    assert deep._rewrite_catalog_search_args("gateway__tool_search", {}, ASKED)["query"] == ASKED


def test_composio_keeps_its_short_phrase():
    conn = SimpleNamespace(provider="composio")
    out = deep._rewrite_catalog_search_args("COMPOSIO_SEARCH_TOOLS", {"query": "one8 inventory left"}, "", conn)
    assert "one8" not in out["query"]


def test_other_tools_are_left_alone():
    assert deep._rewrite_catalog_search_args("get_daily_sales", {"brand_id": "lotto"}, ASKED) == {"brand_id": "lotto"}


async def test_the_catalog_lookup_searches_with_the_full_request_first():
    searched = []

    async def fake_call(slug, tool, args):
        searched.append(args.get("query"))
        return {"candidates": [{"public_name": "get_daily_sales", "description": "Day-level sales",
                                "inputSchema": {"type": "object", "properties": {"brand_id": {}, "since": {}, "until": {}}}}]}  # fmt: skip

    conn = SimpleNamespace(slug="agilitas", name="Agilitas", alias="", kind="mcp", provider="mcp", server_url="https://gw.test/mcp",
                           server_key="mcp:gw", id="c1", health="ok")  # fmt: skip
    ctx = deep.RunContext(db=None, user=SimpleNamespace(id="u1"), connectors={"agilitas": conn}, call_impl=fake_call)
    ctx.gateway_surface["agilitas"] = {"search": "gateway__tool_search", "search_schema": {"type": "object", "properties": {"query": {}}},
                                       "execute": "gateway__execute", "resolve": None, "resolve_schema": {}}  # fmt: skip
    rows = await deep._expand_gateway_catalog(ctx, conn, "One8 vs Lotto sales in last 6 days")
    assert searched[0] == "One8 vs Lotto sales in last 6 days" and rows[0]["tool_name"] == "get_daily_sales"
