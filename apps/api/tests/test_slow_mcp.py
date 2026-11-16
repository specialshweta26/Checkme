"""MCP tools on top of slow dashboards (docs/LLD.md "Slow MCP tools"): a real MCP server over streamable HTTP that
waits before answering, reports progress, returns only structured data, errors, or returns nothing."""

from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver import Context, MCPServer

from app.connectors import mcp_client


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def server(monkeypatch):
    import uvicorn

    app = MCPServer("dashboard")

    @app.tool()
    async def slow_report(seconds: float, ctx: Context) -> dict:
        """Waits like a dashboard loading its data, reporting progress, then returns rows."""
        steps = 4
        for i in range(steps):
            await asyncio.sleep(seconds / steps)
            await ctx.report_progress(i + 1, steps, f"loading {i + 1}/{steps}")
        return {"rows": [{"region": "North", "sales": 120}, {"region": "South", "sales": 95}], "count": 2}

    @app.tool()
    async def broken() -> str:
        """Fails."""
        raise ValueError("dashboard query failed: warehouse offline")

    port = _free_port()
    config = uvicorn.Config(app.streamable_http_app(), host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(config)
    task = asyncio.create_task(srv.serve())
    for _ in range(100):
        if srv.started:
            break
        await asyncio.sleep(0.05)

    async def allow_local(url):
        return None

    monkeypatch.setattr(mcp_client, "validate_public_url", allow_local)
    yield f"http://127.0.0.1:{port}/mcp"
    srv.should_exit = True
    await task


async def test_a_slow_tool_is_waited_for_and_its_progress_reported(server):
    seen = []

    async def progress(done, total, message):
        seen.append((done, total, message))

    out = await mcp_client.call_mcp_tool(server, {}, "slow_report", {"seconds": 2.0}, on_progress=progress)
    assert out["count"] == 2 and out["rows"][0]["region"] == "North"  # structured data, not "ok"
    assert [s[0] for s in seen] == [1, 2, 3, 4] and seen[-1][2] == "loading 4/4"


async def test_a_tool_error_is_raised_not_returned_as_data(server):
    with pytest.raises(RuntimeError, match="Error executing tool broken"):
        await mcp_client.call_mcp_tool(server, {}, "broken", {})


async def test_a_call_past_the_limit_says_so_plainly(server, monkeypatch):
    monkeypatch.setattr(mcp_client, "tool_timeout_s", lambda: 1.0)
    with pytest.raises(RuntimeError, match="did not answer within 1 seconds.*not report it as zero results"):
        await mcp_client.call_mcp_tool(server, {}, "slow_report", {"seconds": 4.0})


def test_results_of_every_shape_are_read():
    text = SimpleNamespace(type="text", text='{"a": 1}')
    assert mcp_client.result_payload(SimpleNamespace(content=[text], structured_content=None, is_error=False)) == {"a": 1}
    structured = SimpleNamespace(content=[], structured_content={"rows": [1, 2]}, is_error=False)
    assert mcp_client.result_payload(structured) == {"rows": [1, 2]}
    camel = {"content": [{"type": "text", "text": "plain words"}], "isError": False}
    assert mcp_client.result_payload(camel) == {"text": "plain words"}
    res = SimpleNamespace(type="resource", resource=SimpleNamespace(uri="file://r.csv", mime_type="text/csv", text="a,b\n1,2"))
    assert mcp_client.result_payload(SimpleNamespace(content=[res], structured_content=None, is_error=False)) == {"text": "a,b\n1,2"}
    link = SimpleNamespace(type="resource_link", uri="https://x/report.pdf", name="report", mime_type="application/pdf")
    assert mcp_client.result_payload(SimpleNamespace(content=[link], structured_content=None, is_error=False))["attachments"][0]["link"].endswith("report.pdf")
    empty = mcp_client.result_payload(SimpleNamespace(content=[], structured_content=None, is_error=False))
    assert empty["empty"] is True and "not the same as zero results" in empty["note"]
    with pytest.raises(RuntimeError, match="boom"):
        mcp_client.result_payload(SimpleNamespace(content=[SimpleNamespace(type="text", text="boom")], structured_content=None, is_error=True))


async def test_the_agent_says_it_is_still_waiting_on_a_slow_connected_app(monkeypatch):
    from app.agent import deep

    monkeypatch.setattr(deep, "SLOW_TOOL_FIRST_S", 0.05)
    monkeypatch.setattr(deep, "SLOW_TOOL_EVERY_S", 0.05)
    lines = []

    class Progress:
        async def emit(self, text, kind="step", tool=""):
            lines.append(text)

    ctx = SimpleNamespace(progress=Progress())
    conn = SimpleNamespace(kind="mcp", server_url="https://dash.test/mcp", alias="", name="Sales dashboard")
    async with deep.waiting_on(ctx, conn, "slow_report"):
        await mcp_client.PROGRESS.get()(1, 4, "loading 1/4")
        await asyncio.sleep(0.2)
    assert lines[0] == "Sales dashboard: loading 1/4 (25%)"
    assert any(line.startswith("Still waiting for Sales dashboard") for line in lines[1:])
    assert mcp_client.PROGRESS.get() is None  # reset after the call
    builtin = SimpleNamespace(kind="builtin", server_url="", name="Gmail")
    async with deep.waiting_on(ctx, builtin, "search"):
        assert mcp_client.PROGRESS.get() is None


def test_errors_inside_task_groups_are_unwrapped():
    inner = TimeoutError("Request 'tools/call' timed out")
    wrapped = ExceptionGroup("unhandled errors in a TaskGroup", [ExceptionGroup("unhandled errors in a TaskGroup", [inner])])
    assert mcp_client.root_error(wrapped) is inner
    assert mcp_client.root_error(ValueError("x")).args == ("x",)


@pytest.mark.skipif(not __import__("os").environ.get("CHECKME_SLOW_TESTS"), reason="set CHECKME_SLOW_TESTS=1 to run the 2-minute call")
async def test_a_two_minute_dashboard_call_returns_its_data(server):
    seen = []

    async def progress(done, total, message):
        seen.append(message)

    out = await mcp_client.call_mcp_tool(server, {}, "slow_report", {"seconds": 125.0}, on_progress=progress)
    assert out["count"] == 2 and len(seen) == 4
