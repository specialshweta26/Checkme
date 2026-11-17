from __future__ import annotations

import asyncio
import json
import logging
import re
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


# --- sessions reused within a turn (docs/LLD.md "MCP sessions per turn") --------------------------------------------
# Opening an MCP session costs an HTTP connection, a TLS handshake and an initialize round trip, often a second or more.
# A turn that calls one connector five times used to pay that five times. Within ``turn_sessions()`` each (server,
# credentials) pair keeps one session for the turn, closed when the turn ends.


def _reuse_enabled() -> bool:
    from app.config import get_settings

    return bool(getattr(get_settings(), "mcp_session_reuse", True))


class _Held:
    """One open session, owned by its own task: the MCP client's task groups must be entered and left in one task,
    while the agent's tool calls run in many."""

    def __init__(self, server_url: str, creds: dict, read_s: float):
        self.stop = asyncio.Event()
        self.ready: asyncio.Future = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self._hold(server_url, creds, read_s))

    async def _hold(self, server_url: str, creds: dict, read_s: float) -> None:
        try:
            async with mcp_session(server_url, creds, read_s=read_s) as session:
                self.ready.set_result(session)
                await self.stop.wait()
        except BaseException as exc:  # noqa: BLE001 - handed to whoever waits for the session
            if not self.ready.done():
                self.ready.set_exception(exc)
            elif not isinstance(exc, asyncio.CancelledError):
                log.info("pooled MCP session for %s ended", server_url, exc_info=True)

    @property
    def alive(self) -> bool:
        return not self.task.done()

    async def close(self) -> None:
        self.stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(self.task), 5)
        except BaseException:  # noqa: BLE001
            self.task.cancel()
        if self.ready.done() and not self.ready.cancelled():
            self.ready.exception()  # mark it retrieved


class SessionPool:
    def __init__(self) -> None:
        self._held: dict[tuple, _Held] = {}
        self._lock = asyncio.Lock()
        self.opened = 0
        self.closed = False  # work that outlives the turn opens its own sessions

    @staticmethod
    def key(server_url: str, creds: dict) -> tuple:
        return (server_url, json.dumps(_headers(creds), sort_keys=True))

    async def get(self, server_url: str, creds: dict, read_s: float) -> tuple[ClientSession, bool]:
        """(session, reused): an open session for this server and credentials, opening one if needed."""
        key = self.key(server_url, creds)
        async with self._lock:
            held = self._held.get(key)
            reused = held is not None and held.alive
            if not reused:
                held = _Held(server_url, creds, read_s)
                self._held[key] = held
                self.opened += 1
        try:
            return await held.ready, reused
        except BaseException:
            await self.drop(server_url, creds, held)
            raise

    async def drop(self, server_url: str, creds: dict, held: "_Held | None" = None) -> None:
        key = self.key(server_url, creds)
        async with self._lock:
            current = self._held.get(key)
            if current is None or (held is not None and current is not held):
                return
            del self._held[key]
        await current.close()

    async def aclose(self) -> None:
        self.closed = True
        async with self._lock:
            held, self._held = list(self._held.values()), {}
        await asyncio.gather(*(h.close() for h in held), return_exceptions=True)


POOL: "ContextVar[SessionPool | None]" = ContextVar("mcp_session_pool", default=None)


def _open_pool() -> "SessionPool | None":
    pool = POOL.get()
    return pool if pool is not None and not pool.closed else None


@asynccontextmanager
async def turn_sessions():
    """Reuse MCP sessions until the block ends. Nested blocks (a teammate's run inside a turn) share the outer pool."""
    if POOL.get() is not None or not _reuse_enabled():
        yield POOL.get()
        return
    pool = SessionPool()
    token = POOL.set(pool)
    try:
        yield pool
    finally:
        POOL.reset(token)
        await pool.aclose()


def _transport_failure(exc: BaseException) -> bool:
    """A broken connection, as opposed to the server answering with an error or a slow call timing out."""
    from mcp.shared import exceptions

    answered = tuple(c for c in (getattr(exceptions, "MCPError", None), getattr(exceptions, "McpError", None)) if c)
    root = root_error(exc)
    if answered and isinstance(root, answered):  # the server answered with an error: it got the call
        return False
    text = f"{type(root).__name__}: {root}".lower()
    return not ("timeout" in text or "timed out" in text)


async def _call_pooled(pool: SessionPool, server_url: str, creds: dict, name: str, args: dict, limit: float, on_progress):
    """Call through the turn's session. A reused session that has gone stale (the server closed it) is reopened once;
    a call on a fresh session is never repeated, so a tool that changes things can't run twice."""
    session, reused = await pool.get(server_url, creds, limit)
    try:
        return await session.call_tool(name, args or {}, read_timeout_seconds=limit, progress_callback=on_progress)
    except Exception as exc:  # noqa: BLE001
        await pool.drop(server_url, creds)
        if not reused or not _transport_failure(exc):
            raise
        log.info("pooled MCP session for %s failed; reconnecting once", server_url)
    session, _ = await pool.get(server_url, creds, limit)
    return await session.call_tool(name, args or {}, read_timeout_seconds=limit, progress_callback=on_progress)


async def list_mcp_tools(server_url: str, creds: dict) -> list:
    pool = _open_pool()
    if pool is not None:
        session, _ = await pool.get(server_url, creds, tool_timeout_s())
        result = await session.list_tools()
        return list(result.tools)
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
    pool = _open_pool()
    try:
        if pool is not None:
            result = await _call_pooled(pool, server_url, creds, name, args, limit, on_progress)
        else:
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


# --- data that isn't ready yet ----------------------------------------------------------------------------------
# Some MCP servers sit on dashboards that load their data on demand (about two minutes): asked before that, they answer
# at once with zeros or a "not ready" flag instead of waiting (docs/LLD.md "Slow MCP tools").
_NOT_READY_TEXT = ("not connected", "not ready", "still loading", "loading data", "try again", "warming up", "being prepared",
                   "no data yet", "data is loading", "processing")  # fmt: skip
# Numbers that label or describe the answer rather than measure the business: an hour or date in a row, an id, a
# page, or a gateway's own bookkeeping (confidence, tool calls, time taken). An all-zero dashboard has these too.
_NOT_A_FIGURE = re.compile(
    r"^(hour|day|date|week|month|year|quarter|weekday|dow|period|index|idx|rank|position|page|limit|offset|id|version|"
    r"confidence|score|hops|tool_calls|calls|errors|attempts|retries|status_code|code)$|_(id|ms|s|seconds|time|at|index)$|"
    r"(duration|latency|elapsed|took|timing)",
)
_NOT_READY_STATUS = {"loading", "pending", "processing", "warming", "queued", "computing", "in_progress", "not_ready", "stale"}


def _walk(value: Any, depth: int = 0):
    if depth > 8:
        return
    if isinstance(value, dict):
        for k, v in value.items():
            yield k, v
            yield from _walk(v, depth + 1)
    elif isinstance(value, list):
        for v in value[:200]:
            yield None, v
            yield from _walk(v, depth + 1)


def looks_not_ready(payload: Any) -> str:
    """Why a tool's answer looks like data that hasn't loaded yet ("" when it looks like a real answer): an explicit flag
    (ready false, needs_compute, a loading status, a 'not connected' / 'still loading' message), an empty answer, or
    every figure zero (three or more numbers)."""
    if isinstance(payload, dict) and payload.get("empty"):
        return "it returned no data"
    numbers: list[float] = []
    for key, value in _walk(payload):
        k = str(key or "").lower()
        if k in {"ready", "is_ready", "loaded", "available"} and value is False:
            return f"it reported {k}=false"
        if k in {"needs_compute", "loading", "is_loading", "pending", "stale"} and value is True:
            return f"it reported {k}"
        if k in {"status", "state", "feed_status", "sync_status"} and str(value).strip().lower().replace(" ", "_") in _NOT_READY_STATUS:
            return f"its status was {value}"
        if isinstance(value, str) and len(value) < 400 and any(p in value.lower() for p in _NOT_READY_TEXT):
            return f"it said “{value[:120]}”"
        if isinstance(value, (int, float)) and not isinstance(value, bool) and not _NOT_A_FIGURE.search(k):
            numbers.append(float(value))
    if len(numbers) >= 3 and all(n == 0 for n in numbers):
        return "every figure was zero"
    return ""


def hostname_slug(url: str) -> str:
    host = urlparse(url).hostname or "mcp"
    return host.replace(".", "-")[:32]
