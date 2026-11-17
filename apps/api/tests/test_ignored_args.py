"""A tool that doesn't recognise an argument's name answers for its defaults (docs/LLD.md "Gateway arguments"): when its
answer covers a wider period than the dates sent, the agent is told, with the action's real parameter names."""

from __future__ import annotations

from types import SimpleNamespace

from app.agent import deep
from app.connectors import mcp_client


def test_an_answer_for_other_dates_than_sent_is_caught():
    sent = {"tool_name": "get_catalog_performance", "arguments": {"brand": "x", "start": "2026-11-16", "end": "2026-11-17"}}
    got = {"connected": True, "range": {"start": "2026-10-19", "end": "2026-11-17"}, "rows": [{"units": 3}]}
    assert "2026-10-19" in mcp_client.ignored_dates(sent, got)
    assert not mcp_client.ignored_dates(sent, {"window": {"start": "2026-11-16", "end": "2026-11-17"}})
    assert not mcp_client.ignored_dates(sent, {"window": {"start": "2026-11-16", "end": "2026-11-16"}})  # clamped: fine
    assert not mcp_client.ignored_dates({"brand": "x"}, got)  # no dates sent: nothing to compare
    assert not mcp_client.ignored_dates(sent, {"rows": []})  # no window in the answer


async def test_ignored_dates_tell_the_agent_is_told_the_real_parameters(monkeypatch):
    calls = []

    async def invoke(db, user, conn, tool_name, args, attachments=None):
        calls.append(args)
        return {"connected": False, "range": {"start": "2026-10-19", "end": "2026-11-17"}}

    async def surface(ctx, conn):
        return {"search": "gateway__tool_search", "execute": "gateway__execute", "resolve": None}

    async def params(ctx, conn, tool_name):
        return {"fields": ["brand", "since", "until"], "required": ["brand"]} if tool_name == "get_catalog_performance" else None

    monkeypatch.setattr(deep, "invoke_named", invoke)
    monkeypatch.setattr(deep, "_load_gateway_surface", surface)
    monkeypatch.setattr(deep, "gateway_action_args", params)
    monkeypatch.setattr(deep, "check_gateway_args", lambda known, tool, args: None)  # let the wrong call through
    conn = SimpleNamespace(kind="mcp", server_url="https://mcp-gateway.example.test/mcp", alias="", name="Gateway",
                           server_key="mcp:gw", id="c1", health="ok", slug="gw")  # fmt: skip
    ctx = deep.RunContext(db=None, user=SimpleNamespace(id="u1"), connectors={"gw": conn}, query="product sales 16-17 Nov")
    out = await deep.call_user_tool(ctx, "gw", "get_catalog_performance", {"brand": "x", "start": "2026-11-16", "end": "2026-11-17"})
    assert sum(1 for c in calls if "tool_name" in c) == 1 and out["args_ignored"] and out["arguments_expected"] == ["brand", "since", "until"]
    assert "brand, since, until" in out["note"]

