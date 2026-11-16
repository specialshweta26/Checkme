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
        ({"status": "Loading", "rows": [1, 2]}, True),
        ({"feed": "product feed not connected", "orders": 12}, True),
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
