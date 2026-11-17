"""MCP tools whose dashboard loads its data on demand (docs/LLD.md "Slow MCP tools"): asked before the data is ready, the
server answers at once with zeros or a not-ready flag. Read-only tools are asked again until the data is there."""

from __future__ import annotations

import asyncio
import socket
import time
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver import MCPServer

from app.agent import deep
from app.connectors import mcp_client

ZEROS = {"net_sales": 0, "gross_sales": 0, "orders": 0, "units": 0, "feed": "One8 product feed: not connected"}
REAL = {"net_sales": 412000, "gross_sales": 455000, "orders": 310, "units": 402}


@pytest.mark.parametrize(
    "payload,flagged",
    [
        ({"net_sales": 0, "orders": 0, "units": 0}, True),
        ({"rows": [{"sales": 0}, {"sales": 0}, {"sales": 0}]}, True),
        ({"ready": False, "data": []}, True),
        ({"needs_compute": True, "rows": []}, True),
        ({"status": "Loading", "rows": [0, 0]}, True),
        ({"status": "Loading", "rows": [1, 2]}, False),
        ({"feed": "product feed not connected", "orders": 0, "units": 0}, True),
        ({"feed": "product feed not connected", "orders": 12}, False),  # real figures win over a side note
        ({"net_sales": 620107, "orders": 261, "web": {"available": False, "sessions": None}}, False),
        ({"status": "Loading", "rows": [{"sales": 0}]}, True),
        ({"ok": True, "empty": True}, True),
        (REAL, False),
        ({"orders": 0, "units": 0}, False),  # too few numbers to tell
        ({"orders": 0, "units": 0, "net_sales": 15}, False),
        ({"text": "Top themes: comfort, sizing"}, False),
        ({"rows": [{"id": 1, "ready": True, "status": "done"}]}, False),
    ],
)
def test_not_ready_answers_are_recognised(payload, flagged):
    assert bool(mcp_client.looks_not_ready(payload)) is flagged


def test_only_reading_tools_are_asked_again():
    assert deep._reads_only("get_daily_sales", None) and deep._reads_only("list_pulse_brands", None)
    assert deep._reads_only("custom_tool", SimpleNamespace(read_only=True, destructive=False))
    assert not deep._reads_only("send_campaign", None) and not deep._reads_only("delete_rows", SimpleNamespace(read_only=False, destructive=True))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def warming_server(monkeypatch):
    """A dashboard MCP: the first call starts loading the data; until it has loaded, every call answers zeros."""
    import uvicorn

    app = MCPServer("dashboard")
    state = {"started": None, "calls": 0}

    @app.tool()
    async def get_daily_sales(brand_id: str = "one8") -> dict:
        """Daily sales from a dashboard that loads on demand."""
        state["calls"] += 1
        if state["started"] is None:
            state["started"] = time.monotonic()
        return REAL if time.monotonic() - state["started"] >= 1.5 else ZEROS

    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app.streamable_http_app(), host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(srv.serve())
    for _ in range(100):
        if srv.started:
            break
        await asyncio.sleep(0.05)

    async def allow_local(url):
        return None

    monkeypatch.setattr(mcp_client, "validate_public_url", allow_local)
    yield SimpleNamespace(url=f"http://127.0.0.1:{port}/mcp", state=state)
    srv.should_exit = True
    await task


class _Progress:
    def __init__(self):
        self.lines = []

    async def emit(self, text, kind="step", tool=""):
        self.lines.append(text)


async def test_a_warming_dashboard_is_asked_again_until_its_data_is_there(warming_server, monkeypatch):
    monkeypatch.setattr(deep, "WARMUP_WAITS_S", (1, 1, 1))
    ctx = SimpleNamespace(progress=_Progress())
    conn = SimpleNamespace(kind="mcp", server_url=warming_server.url, alias="", name="Agilitas")

    def again():
        return mcp_client.call_mcp_tool(warming_server.url, {}, "get_daily_sales", {"brand_id": "one8"})

    first = await again()
    assert first["net_sales"] == 0  # what used to reach the agent
    out = await deep.retry_until_ready(ctx, conn, "get_daily_sales", first, again)
    assert out == REAL and warming_server.state["calls"] >= 2
    assert ctx.progress.lines[0].startswith("Agilitas answered before its data was ready")


async def test_still_not_ready_after_waiting_is_said_plainly(monkeypatch):
    monkeypatch.setattr(deep, "WARMUP_WAITS_S", (0, 0))
    calls = []

    async def again():
        calls.append(1)
        return dict(ZEROS)

    ctx = SimpleNamespace(progress=None)
    conn = SimpleNamespace(kind="mcp", server_url="https://dash.test/mcp", alias="Agilitas", name="x")
    out = await deep.retry_until_ready(ctx, conn, "get_daily_sales", dict(ZEROS), again)
    assert len(calls) == 2 and out["not_ready"] is True
    assert "do not report the figures as real zeros" in out["note"] and out["net_sales"] == 0


async def test_a_real_answer_is_returned_at_once(monkeypatch):
    async def again():
        raise AssertionError("must not ask again")

    out = await deep.retry_until_ready(SimpleNamespace(progress=None), SimpleNamespace(alias="", name="x"), "get_daily_sales", dict(REAL), again)
    assert out == REAL


async def test_the_switch_turns_it_off(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "mcp_warmup_retry", False)

    async def again():
        raise AssertionError("must not ask again")

    out = await deep.retry_until_ready(SimpleNamespace(progress=None), SimpleNamespace(alias="", name="x"), "get_daily_sales", dict(ZEROS), again)
    assert out == ZEROS


async def test_the_agents_tool_call_asks_again_for_a_connected_mcp(monkeypatch):
    monkeypatch.setattr(deep, "WARMUP_WAITS_S", (0,))
    answers = [dict(ZEROS), dict(REAL)]
    calls = []

    async def invoke(db, user, conn, tool_name, args, attachments=None):
        calls.append(tool_name)
        return answers.pop(0)

    async def no_surface(ctx, conn):
        return {}

    monkeypatch.setattr(deep, "invoke_named", invoke)
    monkeypatch.setattr(deep, "_load_gateway_surface", no_surface)
    conn = SimpleNamespace(kind="mcp", server_url="https://mcp-gateway.example.test/mcp", alias="", name="Agilitas",
                           server_key="mcp:agilitas", id="c1", health="ok")  # fmt: skip
    ctx = deep.RunContext(db=None, user=SimpleNamespace(id="u1"), connectors={"agilitas": conn}, auto_confirm=True,
                          query="One8 sales in last 19 days")  # fmt: skip
    out = await deep.call_user_tool(ctx, "agilitas", "get_daily_sales", {"brand_id": "one8"})
    assert calls == ["get_daily_sales", "get_daily_sales"] and out["net_sales"] == 412000


def _dashboard(net: int) -> dict:
    """The shape a sales dashboard answered with (Lotto, 13-17 Nov): KPIs, then a row per hour."""
    kpis = {"net_sales": net, "gross_sales": net, "discounts": 0, "net_orders": net // 1000, "aov": 0, "sessions": None}
    hourly = [{"hour": h, "sessions": 0, "gross_sales": float(net // 24), "orders": 0, "net_sales": net // 24} for h in range(24)]
    return {"brand": "Lotto", "window": {"start": "2026-11-13", "end": "2026-11-17"}, "basis": "forward", "kpis": kpis, "hourly": hourly}


def test_an_all_zero_dashboard_with_hour_rows_is_not_ready():
    """Hours 1-23 are labels, not figures: they used to make an all-zero answer look like data."""
    assert mcp_client.looks_not_ready(_dashboard(0)) == "every figure was zero"
    assert not mcp_client.looks_not_ready(_dashboard(480000))
    wrapped = {"outcome": "executed", "confidence": 97, "tool_calls": 8, "hops": 15, "errors": 0, "gateway_time_ms": 66708,
               "result": _dashboard(0)}  # fmt: skip
    assert mcp_client.looks_not_ready(wrapped)


async def test_a_read_through_a_gateways_execute_is_asked_again(monkeypatch):
    monkeypatch.setattr(deep, "WARMUP_WAITS_S", (0,))
    answers = [_dashboard(0), _dashboard(480000)]
    calls = []

    async def invoke(db, user, conn, tool_name, args, attachments=None):
        calls.append((tool_name, args))
        return answers.pop(0)

    async def surface(ctx, conn):
        return {"search": "gateway__tool_search", "execute": "gateway__execute", "resolve": None}

    async def no_catalog(ctx, conn, tool_name):
        return None

    monkeypatch.setattr(deep, "invoke_named", invoke)
    monkeypatch.setattr(deep, "_load_gateway_surface", surface)
    monkeypatch.setattr(deep, "gateway_action_args", no_catalog)
    conn = SimpleNamespace(kind="mcp", server_url="https://mcp-gateway.example.test/mcp", alias="", name="Agilitas",
                           server_key="mcp:agilitas", id="c1", health="ok", slug="agilitas")  # fmt: skip
    ctx = deep.RunContext(db=None, user=SimpleNamespace(id="u1"), connectors={"agilitas": conn}, auto_confirm=False,
                          query="Lotto sales last 5 days")  # fmt: skip
    out = await deep.call_user_tool(ctx, "agilitas", "get_sales_dashboard", {"brand": "lotto"})
    assert [c[0] for c in calls] == ["gateway__execute", "gateway__execute"] and out["kpis"]["net_sales"] == 480000
    assert not ctx.pending


async def test_a_gateway_action_that_writes_is_never_asked_again(monkeypatch):
    monkeypatch.setattr(deep, "WARMUP_WAITS_S", (0,))
    calls = []

    async def invoke(db, user, conn, tool_name, args, attachments=None):
        calls.append(tool_name)
        return {"sent": 0, "queued": 0, "failed": 0}

    async def surface(ctx, conn):
        return {"search": "gateway__tool_search", "execute": "gateway__execute", "resolve": None}

    async def no_catalog(ctx, conn, tool_name):
        return None

    monkeypatch.setattr(deep, "invoke_named", invoke)
    monkeypatch.setattr(deep, "_load_gateway_surface", surface)
    monkeypatch.setattr(deep, "gateway_action_args", no_catalog)
    conn = SimpleNamespace(kind="mcp", server_url="https://mcp-gateway.example.test/mcp", alias="", name="Agilitas",
                           server_key="mcp:agilitas", id="c1", health="ok", slug="agilitas")  # fmt: skip
    ctx = deep.RunContext(db=None, user=SimpleNamespace(id="u1"), connectors={"agilitas": conn}, auto_confirm=True, query="send it")
    await deep.call_user_tool(ctx, "agilitas", "send_campaign", {"segment": "lapsed"})
    assert calls == ["gateway__execute"]


async def test_an_action_still_empty_after_the_retries_comes_back_with_other_actions(monkeypatch):
    """One action of a gateway can be empty for some inputs while another answering the same question has the data."""
    monkeypatch.setattr(deep, "WARMUP_WAITS_S", (0,))
    calls = []

    async def invoke(db, user, conn, tool_name, args, attachments=None):
        calls.append(args)
        return _dashboard(0)

    async def surface(ctx, conn):
        return {"search": "gateway__tool_search", "execute": "gateway__execute", "resolve": None}

    async def no_catalog(ctx, conn, tool_name):
        return None

    async def catalog(ctx, conn, query):
        return [{"tool_name": "get_sales_dashboard", "arguments": ["brand"], "description": "dashboard"},
                {"tool_name": "get_daily_sales", "arguments": ["brand", "start", "end"], "description": "Day-level sales"}]  # fmt: skip

    monkeypatch.setattr(deep, "invoke_named", invoke)
    monkeypatch.setattr(deep, "_load_gateway_surface", surface)
    monkeypatch.setattr(deep, "gateway_action_args", no_catalog)
    monkeypatch.setattr(deep, "_expand_gateway_catalog", catalog)
    conn = SimpleNamespace(kind="mcp", server_url="https://mcp-gateway.example.test/mcp", alias="", name="Gateway",
                           server_key="mcp:gw", id="c1", health="ok", slug="gw")  # fmt: skip
    ctx = deep.RunContext(db=None, user=SimpleNamespace(id="u1"), connectors={"gw": conn}, query="product wise sales across 7 days")
    out = await deep.call_user_tool(ctx, "gw", "get_sales_dashboard", {"brand": "x"})
    assert out["not_ready"] and [a["tool_name"] for a in out["try_instead"]] == ["get_daily_sales"]
    assert out["try_instead"][0]["arguments"] == ["brand", "start", "end"] and "try_instead" in out["note"]


def test_a_source_that_says_it_is_not_connected_is_not_ready():
    assert mcp_client.looks_not_ready({"connected": False, "range": {"start": "2026-10-19", "end": "2026-11-17"}})
    assert not mcp_client.looks_not_ready({"connected": True, "rows": [{"sku": "A", "units": 4, "sales": 9800}]})


def test_an_answer_for_other_dates_than_sent_is_caught():
    sent = {"tool_name": "get_catalog_performance", "arguments": {"brand": "x", "start": "2026-11-16", "end": "2026-11-17"}}
    got = {"connected": True, "range": {"start": "2026-10-19", "end": "2026-11-17"}, "rows": [{"units": 3}]}
    assert "2026-10-19" in mcp_client.ignored_dates(sent, got)
    assert not mcp_client.ignored_dates(sent, {"window": {"start": "2026-11-16", "end": "2026-11-17"}})
    assert not mcp_client.ignored_dates(sent, {"window": {"start": "2026-11-16", "end": "2026-11-16"}})  # clamped: fine
    assert not mcp_client.ignored_dates({"brand": "x"}, got)  # no dates sent: nothing to compare
    assert not mcp_client.ignored_dates(sent, {"rows": []})  # no window in the answer


async def test_ignored_dates_are_not_retried_and_the_agent_is_told_the_real_parameters(monkeypatch):
    monkeypatch.setattr(deep, "WARMUP_WAITS_S", (0,))
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
