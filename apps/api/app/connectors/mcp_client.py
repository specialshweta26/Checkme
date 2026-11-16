from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from app.connectors.urlsafety import validate_public_url

log = logging.getLogger(__name__)

# Some MCP servers sit on top of slow dashboards or reports and take minutes to answer (docs/LLD.md "Slow MCP
# tools"). The HTTP read and the tool call both wait up to MCP_TOOL_TIMEOUT_S; connecting stays quick.
CONNECT_TIMEOUT_S = 30.0
MAX_TEXT = 60000

ProgressFn = Callable[[float, "float | None", "str | None"], Awaitable[None]]
# Set by the agent while it waits on a tool, so progress the server reports reaches the chat.
PROGRESS: "ContextVar[ProgressFn | None]" = ContextVar("mcp_progress", default=None)


def tool_timeout_s() -> float:
    from app.config import get_settings

    try:
        return max(30.0, float(getattr(get_settings(), "mcp_tool_timeout_s", 600) or 600))
    except (TypeError, ValueError):
        return 600.0


def _headers(creds: dict) -> dict[str, str]:
    token = creds.get("access_token")
    if not token:
        return {}
    header = creds.get("header") or "Authorization"
    prefix = creds.get("prefix", "Bearer ")
    return {header: f"{prefix}{token}"}


def _timeout(read_s: float):
    import httpx2

    return httpx2.Timeout(CONNECT_TIMEOUT_S, read=read_s)


@asynccontextmanager
async def mcp_session(server_url: str, creds: dict, read_s: float | None = None):
    await validate_public_url(server_url)
    headers = _headers(creds)
    async with create_mcp_http_client(headers=headers or None, timeout=_timeout(read_s or tool_timeout_s())) as http:
        async with streamable_http_client(server_url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def list_mcp_tools(server_url: str, creds: dict) -> list:
    async with mcp_session(server_url, creds) as session:
        result = await session.list_tools()
        return list(result.tools)


def _field(obj: Any, *names: str) -> Any:
    """A field under either spelling: newer SDKs use snake_case (``is_error``), older ones camelCase (``isError``)."""
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def result_payload(result: Any) -> Any:
    """What an MCP tool returned, for the agent: structured data first, then text, embedded resources and links.
    Raises RuntimeError when the server marked the call as an error. Returns {"ok": True, "empty": True, ...} when the
    tool sent nothing at all, so "no data" is never mistaken for an answer."""
    structured = _field(result, "structured_content", "structuredContent")
    texts: list[str] = []
    extras: list[dict] = []
    for block in _field(result, "content") or []:
        kind = str(_field(block, "type") or "")
        if kind == "text":
            texts.append(str(_field(block, "text") or ""))
        elif kind == "resource":
            res = _field(block, "resource")
            text = _field(res, "text")
            if text:
                texts.append(str(text))
            else:
                extras.append({"resource": str(_field(res, "uri") or ""), "mime": str(_field(res, "mime_type", "mimeType") or "")})
        elif kind == "resource_link":
            extras.append({"link": str(_field(block, "uri") or ""), "name": str(_field(block, "name") or ""),
                           "mime": str(_field(block, "mime_type", "mimeType") or "")})  # fmt: skip
        elif kind in {"image", "audio"}:
            extras.append({kind: True, "mime": str(_field(block, "mime_type", "mimeType") or "")})
    joined = "\n".join(t for t in texts if t)
    if _field(result, "is_error", "isError"):
        raise RuntimeError(joined[:2000] or "The MCP tool reported an error without a message")
    if structured not in (None, {}, []):
        if isinstance(structured, dict) and joined and len(joined) <= 2000 and "text" not in structured:
            return {**structured, "text": joined}
        return structured
    if joined:
        try:
            parsed = json.loads(joined)
        except json.JSONDecodeError:
            parsed = None
        if parsed is not None:
            return parsed
        out: dict[str, Any] = {"text": joined[:MAX_TEXT]}
        if len(joined) > MAX_TEXT:
            out["truncated"] = len(joined) - MAX_TEXT
        if extras:
            out["attachments"] = extras
        return out
    if extras:
        return {"ok": True, "attachments": extras}
    return {"ok": True, "empty": True, "note": "The tool answered with no data at all. This is not the same as zero results: say so, and retry or check the source if data was expected."}


async def call_mcp_tool(server_url: str, creds: dict, name: str, args: dict[str, Any], on_progress: ProgressFn | None = None) -> Any:
    """Call one MCP tool and wait as long as it takes, up to MCP_TOOL_TIMEOUT_S. Progress the server reports is passed
    to ``on_progress`` (asking for it also keeps long calls alive on servers that send it)."""
    limit = tool_timeout_s()
    on_progress = on_progress or PROGRESS.get()
    try:
        async with mcp_session(server_url, creds, read_s=limit) as session:
            result = await session.call_tool(name, args or {}, read_timeout_seconds=limit, progress_callback=on_progress)
    except Exception as exc:  # noqa: BLE001
        root = root_error(exc)
        text = f"{type(root).__name__}: {root}".lower()
        if "timeout" in text or "timed out" in text:
            raise RuntimeError(
                f"The MCP server did not answer within {int(limit)} seconds. Its data source may be slow or down; "
                "tell the user this, do not report it as zero results."
            ) from exc
        message = str(root).strip()
        raise RuntimeError(f"The MCP call failed: {message[:500]}" if message else f"The MCP call failed ({type(root).__name__}) without a message") from exc
    return result_payload(result)


def root_error(exc: BaseException) -> BaseException:
    """The error underneath the MCP client's task groups: it wraps every failure in an ExceptionGroup whose own
    message ("unhandled errors in a TaskGroup") says nothing about what went wrong."""
    seen = 0
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions and seen < 10:
        exc = exc.exceptions[0]
        seen += 1
    return exc


def hostname_slug(url: str) -> str:
    host = urlparse(url).hostname or "mcp"
    return host.replace(".", "-")[:32]
