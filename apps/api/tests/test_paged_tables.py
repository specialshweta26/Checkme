"""Whole tables from a connector (docs/LLD.md "Paged tables"): every page is read, and totals come from every row."""

from __future__ import annotations

from types import SimpleNamespace

from app.agent import deep, tables


def _row(i: int) -> list:
    channel = "SIS" if i % 3 == 0 else "EBO"
    category = "Closed Footwear" if i % 2 else "Apparel"
    return [f"2026-11-{1 + i % 17:02d}", channel, channel, "BrandA", category, "Men", 1, 2000, 1500, 1300.0]


TOTAL = 9


def _page(offset: int, limit: int) -> dict:
    rows = [_row(i) for i in range(offset, min(offset + limit, TOTAL))]
    return {"ok": True, "key": "daily", "totalRows": TOTAL, "returnedRows": len(rows), "offset": offset, "limit": limit,
            "hasMore": offset + len(rows) < TOTAL, "data": rows}  # fmt: skip


async def test_every_page_is_read_and_merged():
    asked = []

    async def call(args):
        asked.append(args)
        return _page(args["offset"], min(args["limit"], 4))  # the tool caps pages at 4 rows

    out = await tables.all_pages(_page(0, 3), {"from_date": "2026-11-01"}, call)
    assert out["returnedRows"] == TOTAL and len(out["data"]) == TOTAL and out["hasMore"] is False and out["pages_fetched"] == 3
    assert asked[0]["offset"] == 3 and asked[0]["from_date"] == "2026-11-01"


async def test_an_unpaged_answer_is_left_alone():
    async def call(args):
        raise AssertionError("no more pages")

    one = {"rows": [[1, 2]], "totalRows": 1, "offset": 0}
    assert await tables.all_pages(one, {}, call) == one


def test_totals_and_breakdowns_cover_every_row():
    data = {"data": [_row(i) for i in range(TOTAL)]}
    out = tables.aggregate(data, where={"1": "SIS"}, group_by=[4], sums=[6, 8])
    sis = [r for r in data["data"] if r[1] == "SIS"]
    assert out["rows_matched"] == len(sis) and out["totals"] == {"6": len(sis), "8": 1500 * len(sis)}
    assert sum(g["rows"] for g in out["groups"]) == len(sis)
    dated = tables.aggregate(data, where={"0": {"gte": "2026-11-03", "lte": "2026-11-05"}}, sums=[8])
    assert dated["rows_matched"] == 3


def test_the_gateways_own_plan_is_kept_and_shown():
    conn = SimpleNamespace(slug="gw")
    ctx = deep.RunContext(db=None, user=SimpleNamespace(id="u1"), connectors={"gw": conn})
    deep._keep_gateway_guide(ctx, conn, {"client_prompt": "Call srv__get_daily with from_date and to_date, filter to SIS.",
                                         "trace_id": "t-1", "matches": [{"public_name": "srv__get_daily", "confidence": 0.96, "reason": "daily"}]})  # fmt: skip
    row = deep._gateway_guide_row(ctx, conn)
    assert "from_date" in row["gateway_advice"] and row["recommended"][0]["public_name"] == "srv__get_daily"
    schema = {"type": "object", "properties": {"tool_name": {}, "arguments": {}, "trace_id": {}}}
    assert deep._with_trace(ctx, conn, schema, {"tool_name": "x", "arguments": {}})["trace_id"] == "t-1"
    assert "trace_id" not in deep._with_trace(ctx, conn, {"properties": {"tool_name": {}}}, {"tool_name": "x"})


async def test_the_agents_call_reads_every_page(monkeypatch):
    sent = []

    async def invoke(db, user, conn, tool_name, args, attachments=None):
        sent.append(args)
        inner = args.get("arguments") or {}
        return _page(int(inner.get("offset", 0)), min(int(inner.get("limit", 3)), 4))

    async def surface(ctx, conn):
        return {"search": "gateway__tool_search", "execute": "gateway__execute", "execute_schema": {}, "resolve": None}

    async def no_catalog(ctx, conn, tool_name):
        return None

    monkeypatch.setattr(deep, "invoke_named", invoke)
    monkeypatch.setattr(deep, "_load_gateway_surface", surface)
    monkeypatch.setattr(deep, "gateway_action_args", no_catalog)
    conn = SimpleNamespace(kind="mcp", server_url="https://gw.example.test/mcp", alias="", name="Gateway", server_key="mcp:gw",
                           id="c1", health="ok", slug="gw", provider="mcp")  # fmt: skip
    ctx = deep.RunContext(db=None, user=SimpleNamespace(id="u1"), connectors={"gw": conn}, auto_confirm=True, query="SIS sales MTD")
    out = await deep.call_user_tool(ctx, "gw", "get_data_daily", {"from_date": "2026-11-01", "limit": 3})
    stored = ctx.results[-1]["data"]
    assert len(stored["data"]) == TOTAL and len(sent) == 3
    assert sent[1]["tool_name"] == "get_data_daily" and sent[1]["arguments"]["offset"] == 3
    assert out["data"] == stored["data"] or out.get("partial")
