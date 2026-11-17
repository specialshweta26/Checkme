"""One MCP session per connector for a whole turn (docs/LLD.md "MCP sessions per turn"), against a real MCP server
over streamable HTTP."""

from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager

import pytest
from mcp.server.mcpserver import MCPServer

from app.connectors import mcp_client


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def server(monkeypatch):
    import uvicorn

    app = MCPServer("dashboard")
    calls = {"sales": 0, "send": 0}

    @app.tool()
    async def get_sales(brand_id: str) -> dict:
        """Sales for a brand."""
        calls["sales"] += 1
        await asyncio.sleep(0.05)
        return {"brand_id": brand_id, "net_sales": 412000}

    @app.tool()
    async def send_campaign(name: str) -> str:
        """Sends a campaign; fails."""
        calls["send"] += 1
        raise ValueError("campaign service rejected it")

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
    opened = []
    real = mcp_client.mcp_session

    @asynccontextmanager
    async def counting(url, creds, read_s=None):
        opened.append(url)
        async with real(url, creds, read_s=read_s) as session:
            yield session

    monkeypatch.setattr(mcp_client, "mcp_session", counting)
    yield type("S", (), {"url": f"http://127.0.0.1:{port}/mcp", "opened": opened, "calls": calls})
    srv.should_exit = True
    await task


async def test_calls_in_a_turn_share_one_session(server):
    async with mcp_client.turn_sessions() as pool:
        first = await mcp_client.call_mcp_tool(server.url, {}, "get_sales", {"brand_id": "one8"})
        # parallel calls from other tasks, as the agent's tools run
        rest = await asyncio.gather(*(asyncio.create_task(mcp_client.call_mcp_tool(server.url, {}, "get_sales", {"brand_id": b}))
                                      for b in ("lotto", "hoka", "one8global")))  # fmt: skip
        tools = await mcp_client.list_mcp_tools(server.url, {})
    assert first["net_sales"] == 412000 and [r["brand_id"] for r in rest] == ["lotto", "hoka", "one8global"]
    assert {t.name for t in tools} == {"get_sales", "send_campaign"}
    assert len(server.opened) == 1 and pool.opened == 1 and pool.closed


async def test_other_credentials_get_their_own_session(server):
    async with mcp_client.turn_sessions():
        await mcp_client.call_mcp_tool(server.url, {"access_token": "a"}, "get_sales", {"brand_id": "one8"})
        await mcp_client.call_mcp_tool(server.url, {"access_token": "b"}, "get_sales", {"brand_id": "one8"})
        await mcp_client.call_mcp_tool(server.url, {"access_token": "a"}, "get_sales", {"brand_id": "one8"})
    assert len(server.opened) == 2


async def test_outside_a_turn_every_call_opens_its_own(server):
    for _ in range(2):
        await mcp_client.call_mcp_tool(server.url, {}, "get_sales", {"brand_id": "one8"})
    assert len(server.opened) == 2


async def test_a_stale_session_is_reopened_once(server):
    async with mcp_client.turn_sessions() as pool:
        await mcp_client.call_mcp_tool(server.url, {}, "get_sales", {"brand_id": "one8"})
        session, reused = await pool.get(server.url, {}, 60)
        assert reused

        async def dropped(*a, **k):
            import httpx2

            raise httpx2.RemoteProtocolError("Server disconnected without sending a response.")

        session.call_tool = dropped  # the server closed the connection between calls
        out = await mcp_client.call_mcp_tool(server.url, {}, "get_sales", {"brand_id": "lotto"})
    assert out["brand_id"] == "lotto" and len(server.opened) == 2


async def test_a_tool_that_fails_is_not_run_again(server):
    async with mcp_client.turn_sessions():
        await mcp_client.call_mcp_tool(server.url, {}, "get_sales", {"brand_id": "one8"})
        with pytest.raises(RuntimeError, match="send_campaign"):
            await mcp_client.call_mcp_tool(server.url, {}, "send_campaign", {"name": "Diwali"})
        again = await mcp_client.call_mcp_tool(server.url, {}, "get_sales", {"brand_id": "one8"})
    assert server.calls["send"] == 1 and again["net_sales"] == 412000


async def test_a_nested_turn_shares_the_outer_sessions(server):
    async with mcp_client.turn_sessions() as outer:
        await mcp_client.call_mcp_tool(server.url, {}, "get_sales", {"brand_id": "one8"})
        async with mcp_client.turn_sessions() as inner:
            await mcp_client.call_mcp_tool(server.url, {}, "get_sales", {"brand_id": "lotto"})
        assert inner is outer and not outer.closed
    assert len(server.opened) == 1


async def test_work_after_the_turn_opens_its_own_session(server):
    async with mcp_client.turn_sessions():
        await mcp_client.call_mcp_tool(server.url, {}, "get_sales", {"brand_id": "one8"})
    await mcp_client.call_mcp_tool(server.url, {}, "get_sales", {"brand_id": "one8"})  # pool already closed
    assert len(server.opened) == 2


async def test_the_switch_turns_it_off(server, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "mcp_session_reuse", False)
    async with mcp_client.turn_sessions() as pool:
        for _ in range(2):
            await mcp_client.call_mcp_tool(server.url, {}, "get_sales", {"brand_id": "one8"})
    assert pool is None and len(server.opened) == 2
