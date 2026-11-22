"""Tool search speed (docs/LLD.md "Tool search speed"): each gateway's catalog is searched at the same time, a
search for the same words runs once per turn, and the other wordings are tried together only when the first finds
nothing."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from app.agent import deep

SURFACE = {"search": "gateway__tool_search", "search_schema": {"type": "object", "properties": {"query": {}}},
           "execute": "gateway__execute", "resolve": None, "resolve_schema": {}}  # fmt: skip


def _conn(slug):
    return SimpleNamespace(slug=slug, name=slug, alias="", kind="mcp", provider="mcp", server_url=f"https://{slug}.test/mcp",
                           server_key=f"mcp:{slug}", id=slug, health="ok")  # fmt: skip


def _ctx(found=True):
    state = {"calls": [], "now": 0, "peak": 0}

    async def fake_call(slug, tool, args):
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        state["calls"].append((slug, args.get("query")))
        await asyncio.sleep(0.2)
        state["now"] -= 1
        if not found:
            return {"candidates": []}
        return {"candidates": [{"public_name": f"{slug}__daily_sales", "description": "Day-level sales", "inputSchema": {"type": "object", "properties": {"brand": {}}}}]}

    conns = {s: _conn(s) for s in ("gw-a", "gw-b")}
    ctx = deep.RunContext(db=None, user=SimpleNamespace(id="u1"), connectors=conns, call_impl=fake_call)
    for s in conns:
        ctx.gateway_surface[s] = dict(SURFACE)
    return ctx, state


async def test_every_gateway_is_searched_at_the_same_time():
    ctx, state = _ctx()
    started = time.monotonic()
    rows = await deep.search_user_tools(ctx, "One8 seam shoes sales last 2 weeks")
    assert time.monotonic() - started < 0.35 and state["peak"] == 2  # two 0.2s searches, not 0.4s
    assert {r["connector_slug"] for r in rows if "tool_name" in r} >= {"gw-a", "gw-b"}


async def test_the_same_search_runs_once_per_turn():
    ctx, state = _ctx()
    await deep._expand_gateway_catalog(ctx, ctx.connectors["gw-a"], "One8 sales last 2 weeks")
    await deep._expand_gateway_catalog(ctx, ctx.connectors["gw-a"], "One8 sales last 2 weeks")
    assert len(state["calls"]) == 1


async def test_other_wordings_only_when_the_first_finds_nothing_and_then_together():
    ctx, state = _ctx(found=False)
    await deep._expand_gateway_catalog(ctx, ctx.connectors["gw-a"], "In One8, how are Seam shoes performing in sales in the last 2 weeks?")
    assert len(state["calls"]) >= 2 and state["peak"] == len(state["calls"]) - 1  # first alone, the rest at once
    ctx, state = _ctx(found=True)
    await deep._expand_gateway_catalog(ctx, ctx.connectors["gw-a"], "In One8, how are Seam shoes performing in sales in the last 2 weeks?")
    assert len(state["calls"]) == 1
