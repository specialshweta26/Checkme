"""A gateway action its own catalog marks read-only runs without an approval card, whatever its name
(docs/LLD.md "Gateway arguments"). Writes, and actions nobody vouched for, still ask."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent import approval, deep
from app.connectors.shape import catalog_read_only, slim_search

OTIF = {"public_name": "otif_snapshot", "description": "OTIF for a date range.", "annotations": {"readOnlyHint": True},
        "inputSchema": {"type": "object", "properties": {"date": {}}}}  # fmt: skip
WORDS = {"public_name": "fill_rate_today", "description": "Read-only. Today's fill rate by channel.",
         "inputSchema": {"type": "object", "properties": {"date": {}}}}  # fmt: skip
BLAST = {"public_name": "campaign_blast", "description": "Sends a WhatsApp campaign to a segment.",
         "inputSchema": {"type": "object", "properties": {"segment": {}}}}  # fmt: skip


@pytest.mark.parametrize(
    "item,read",
    [
        (OTIF, True),
        (WORDS, True),
        (BLAST, False),
        ({"public_name": "x", "read_only": True}, True),
        ({"public_name": "x", "annotations": {"readOnlyHint": True, "destructiveHint": True}}, False),
        ({"public_name": "x", "description": "This is not read-only: it refreshes the cache."}, False),
        ({"public_name": "x", "annotations": {"readOnlyHint": False}, "description": "read-only view"}, False),
    ],
)
def test_the_catalogs_read_only_mark_is_recognised(item, read):
    assert catalog_read_only(item) is read
    assert bool(slim_search({"candidates": [item]})["candidates"][0].get("read_only")) is read


def test_the_action_is_found_under_other_keys():
    assert approval.nested_actions("gateway__execute", {"tool": "otif_snapshot", "arguments": {}}) == ["otif_snapshot"]
    assert approval.gateway_needs_approval("gateway__execute", {"tool_name": "otif_snapshot"})
    assert not approval.gateway_needs_approval("gateway__execute", {"tool_name": "otif_snapshot"}, {"otif_snapshot"})
    assert approval.gateway_needs_approval("gateway__execute", {"tool_name": "otif_snapshot", "name": "send_x"}, {"otif_snapshot"})


def _gateway(calls):
    async def fake_call(slug, tool, args):
        calls.append((tool, dict(args)))
        if tool == "gateway__tool_search":
            q = str(args.get("query") or "")
            return {"candidates": [c for c in (OTIF, WORDS, BLAST) if c["public_name"].split("_")[0] in q.replace(" ", "_")] or [OTIF, WORDS, BLAST]}
        return {"otif_pct": 93.4}

    conn = SimpleNamespace(slug="agilitas", name="Agilitas", alias="", kind="mcp", provider="mcp", server_url="https://gw.test/mcp",
                           server_key="mcp:gw", id="c1", health="ok")  # fmt: skip
    ctx = deep.RunContext(db=None, user=SimpleNamespace(id="u1"), connectors={"agilitas": conn}, auto_confirm=False, call_impl=fake_call,
                          query="Otif today")  # fmt: skip
    ctx.gateway_surface["agilitas"] = {"search": "gateway__tool_search", "search_schema": {"type": "object", "properties": {"query": {}}},
                                       "execute": "gateway__execute", "resolve": None, "resolve_schema": {}}  # fmt: skip
    return ctx


async def test_a_read_only_action_runs_without_a_card():
    calls = []
    ctx = _gateway(calls)
    out = await deep.call_user_tool(ctx, "agilitas", "otif_snapshot", {"date": "2026-11-17"})
    assert out["otif_pct"] == 93.4 and not ctx.pending


async def test_calling_execute_directly_with_a_read_only_action_runs_too():
    calls = []
    ctx = _gateway(calls)
    out = await deep.call_user_tool(ctx, "agilitas", "gateway__execute", {"tool_name": "otif_snapshot", "arguments": {"date": "2026-11-17"}})
    assert out["otif_pct"] == 93.4 and not ctx.pending


async def test_an_action_that_writes_still_asks():
    calls = []
    ctx = _gateway(calls)
    out = await deep.call_user_tool(ctx, "agilitas", "campaign_blast", {"segment": "lapsed"})
    assert out["status"] == "awaiting_user_confirmation" and len(ctx.pending) == 1
    assert not any(tool == "gateway__execute" for tool, _ in calls)
