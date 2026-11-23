"""A connector named loosely resolves to the real one (deep.resolve_slug). "not connected for this user" for a near
miss made the agent report a healthy connector as down and give up."""

from __future__ import annotations

from types import SimpleNamespace

from app.agent import deep

GW = "mcp-gateway-agilitas-digital-2"


def _ctx():
    conns = {GW: SimpleNamespace(name="Agilitas Digital", alias="agilitas"), "gmail": SimpleNamespace(name="Gmail", alias="")}
    return SimpleNamespace(connectors=conns)


def test_loose_names_find_the_connector():
    ctx = _ctx()
    assert deep.resolve_slug(ctx, GW) == (GW, "")
    assert deep.resolve_slug(ctx, f"{GW}__funnel-analytics") == (GW, "funnel-analytics")
    assert deep.resolve_slug(ctx, "Agilitas") == (GW, "")
    assert deep.resolve_slug(ctx, "agilitas-digital") == (GW, "")
    assert deep.resolve_slug(ctx, "Agilitas Digital") == (GW, "")
    assert deep.resolve_slug(ctx, "shopify") == ("", "")


async def test_an_unknown_name_lists_the_real_ones_and_is_not_called_an_outage():
    ctx = deep.RunContext(db=None, user=SimpleNamespace(id="u1"), connectors=_ctx().connectors)
    out = await deep.call_user_tool(ctx, "shopify", "get_orders", {})
    assert "not an outage" in out["error"] and out["connected"] == sorted(ctx.connectors)
