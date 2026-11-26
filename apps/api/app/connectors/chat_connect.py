"""Connect a remote MCP server from chat (docs/LLD.md "Connect an MCP server from chat").

The ``connect_mcp_server`` harness tool adds an MCP connector for the user from a URL, through the same steps the
Connectors page uses (``routes.add_connector``):

1. The URL must pass ``validate_public_url``.
2. Adding a connector changes the user's account, so it needs an approval card, unless the user's own message this
   turn names that server's URL (and nothing read this turn tried to give instructions). Unattended runs never add one.
3. Lumio connects and lists the server's tools. A server that answers is saved with its tool index, and the reply
   names the connector and its tool count.
4. A server that wants sign-in gets a pending connector and a connect card: its OAuth flow when it advertises one
   (``/connectors/{id}/oauth/start``), otherwise a key field on the card (``/connectors/{id}/credentials``). Secrets are
   typed into the card, never into chat.

Nothing here knows any particular server.
"""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlparse

from sqlalchemy import select

from app.agent.confirm import sign_confirm
from app.connectors.urlsafety import UnsafeUrl, validate_public_url

log = logging.getLogger(__name__)

CONNECTORS_SLUG = "__checkme_connectors__"
TOOL = "connect_mcp_server"
PROBE_TIMEOUT_S = 45.0
_URLISH = re.compile(r"(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?(?:/[^\s<>\"'`)\]]*)?", re.I)
_AUTH_WORDS = ("401", "403", "unauthorized", "unauthorised", "forbidden", "authentication", "api key", "apikey",
               "access token", "invalid token", "missing token", "bearer")  # fmt: skip


def normalize_url(url: str) -> str:
    raw = (url or "").strip().strip("<>\"'")
    if raw and "://" not in raw:
        raw = "https://" + raw
    return raw


def _key(url: str) -> str:
    """Host, port and path, lower-cased, without the scheme or a trailing slash: what makes two spellings the same."""
    p = urlparse(normalize_url(url))
    host = (p.hostname or "").lower()
    port = f":{p.port}" if p.port and p.port not in (80, 443) else ""
    return f"{host}{port}{p.path.rstrip('/')}".lower()


def named_in_message(url: str, query: str) -> bool:
    """The user's own message this turn names this server's URL (with or without https://)."""
    want = _key(url)
    if not want:
        return False
    for found in _URLISH.findall(query or ""):
        if _key(found.rstrip(".,;:!?")) == want:
            return True
    return False


def _unattended(ctx) -> bool:
    return bool(getattr(ctx, "auto_confirm", False) or getattr(ctx, "job", None) is not None)


def _already_confirmed(ctx, args: dict) -> bool:
    from app.agent import approval

    return approval.find_approval(getattr(ctx, "confirmations", None) or [], CONNECTORS_SLUG, TOOL, args) is not None


def _short(exc: BaseException) -> str:
    leaves = _leaves(exc)
    text = "; ".join(dict.fromkeys(f"{type(e).__name__}: {e}" if str(e) else type(e).__name__ for e in leaves))
    return text[:300] or type(exc).__name__


def _leaves(exc: BaseException) -> list[BaseException]:
    """The real errors inside an exception group (the MCP client raises them from task groups) and their causes."""
    out: list[BaseException] = []
    stack = [exc]
    seen: set[int] = set()
    while stack:
        e = stack.pop()
        if id(e) in seen:
            continue
        seen.add(id(e))
        inner = getattr(e, "exceptions", None)
        if isinstance(inner, (list, tuple)) and inner:
            stack.extend(inner)
            continue
        out.append(e)
        if e.__cause__ is not None:
            stack.append(e.__cause__)
    return out


def needs_auth(exc: BaseException | None) -> bool:
    """The server refused us for missing or wrong credentials (HTTP 401/403, or an error that says so)."""
    if exc is None:
        return False
    for e in _leaves(exc):
        status = getattr(getattr(e, "response", None), "status_code", None) or getattr(e, "status_code", None)
        if status in (401, 403):
            return True
        text = str(e).lower()
        if any(w in text for w in _AUTH_WORDS):
            return True
    return False


# --- seams the tests replace --------------------------------------------------------------------------------------


async def probe_tools(url: str, creds: dict) -> list:
    """Connect and list the server's tools, without waiting minutes on a server that never answers."""
    from app.connectors.mcp_client import mcp_session

    async def _list() -> list:
        async with mcp_session(url, creds, read_s=PROBE_TIMEOUT_S) as session:
            return list((await session.list_tools()).tools)

    return await asyncio.wait_for(_list(), PROBE_TIMEOUT_S + 15)


async def find_existing(db, user_id, url: str):
    from app.models import Connector

    rows = (
        await db.execute(select(Connector).where(Connector.user_id == user_id, Connector.kind == "mcp"))
    ).scalars().all()
    want = _key(url)
    return next((r for r in rows if _key(r.server_url or "") == want), None)


async def save_row(db, user, url: str, name: str, existing, auth_type: str, creds: dict | None):
    """Create (or reuse) the user's connector row for this server, as ``routes.add_connector`` does."""
    from app.connectors.mcp_client import hostname_slug
    from app.connectors.registry import server_key_for
    from app.connectors.service import next_slug
    from app.models import Connector
    from app.security import encrypt

    row = existing
    if row is None:
        label = (name or "").strip() or hostname_slug(url)
        row = Connector(
            user_id=user.id,
            kind="mcp",
            provider="mcp",
            name=label,
            slug=await next_slug(db, user.id, label),
            description="",
            server_url=url,
            transport="streamable_http",
            server_key=server_key_for("mcp", "mcp", url),
            health="pending",
        )
        db.add(row)
        await db.flush()
    row.auth_type = auth_type
    row.credentials_enc = encrypt(creds) if creds else None
    row.health = "pending"
    await db.commit()
    return row


async def index_tools(db, row, tools: list) -> None:
    from app.connectors.service import index_mcp_tools

    await index_mcp_tools(db, row, tools)


async def tool_count(db, row) -> int | None:
    from app.models import ServerCard

    if not getattr(row, "server_key", None):
        return None
    card = await db.get(ServerCard, row.server_key)
    return card.tool_count if card is not None else None


def has_credentials(row) -> bool:
    """The row holds working credentials (a key, or finished OAuth), not a sign-in that never completed."""
    from app.security import decrypt_json

    try:
        creds = decrypt_json(row.credentials_enc) if row.credentials_enc else {}
    except Exception:  # noqa: BLE001
        return False
    return bool(creds) and not creds.get("pending")


async def resync(db, row) -> None:
    from app.connectors.service import sync_connector

    await sync_connector(db, row)


async def discover_auth(url: str) -> dict:
    from app.connectors.mcp_oauth import discover_auth as _discover

    return await _discover(url)


async def register_client(endpoint: str) -> dict:
    from app.connectors.mcp_oauth import register_client as _register

    return await _register(endpoint)


# --- the tool -----------------------------------------------------------------------------------------------------


async def connect_mcp_server(ctx, url: str, name: str = "") -> dict:
    """Add a remote MCP server for this user, asking first unless their message this turn names the URL."""
    if getattr(ctx, "db", None) is None or getattr(ctx, "user", None) is None:
        return {"error": "Connectors can only be added from a signed-in chat."}
    try:
        url = await validate_public_url(normalize_url(url))
    except UnsafeUrl as exc:
        return {"error": f"That URL can't be used for a connector: {exc}"}
    existing = await find_existing(ctx.db, ctx.user.id, url)
    if existing is not None and existing.health == "ok":
        count = await tool_count(ctx.db, existing)
        return {
            "status": "already_connected",
            "connector": existing.alias or existing.name,
            "slug": existing.slug,
            "tool_count": count,
            "message": f"{existing.alias or existing.name} is already connected"
            + (f" with {count} tools." if count is not None else "."),
        }
    args = {"url": url, "name": (name or "").strip()}
    asked = named_in_message(url, getattr(ctx, "query", "")) and not getattr(ctx, "injections", None)
    if not (asked and not _unattended(ctx)) and not _already_confirmed(ctx, args):
        if _unattended(ctx):
            return {"error": "Adding a connector needs the user's approval in chat, so it can't run from a scheduled or background task."}
        host = urlparse(url).hostname or url
        preview = {"text": f"Add the MCP server at {url} as a connector. Lumio will connect to {host} and list its tools."}
        token = sign_confirm("tool", {"slug": CONNECTORS_SLUG, "tool": TOOL, "args": args})
        ctx.pending.append({"needs_confirm": True, "token": token, "title": f"Add connector: {host}", "preview": preview})
        return {"status": "awaiting_user_confirmation", "preview": preview}
    return await add_server(ctx, url, args["name"], existing)


async def run_confirmed(ctx, tool: str, args: dict) -> dict:
    """Run a connector addition the user approved on its card."""
    if tool != TOOL:
        return {"error": f"unknown connector action {tool}"}
    if getattr(ctx, "db", None) is None or getattr(ctx, "user", None) is None:
        return {"error": "Connectors can only be added from a signed-in chat."}
    try:
        url = await validate_public_url(normalize_url(str((args or {}).get("url") or "")))
    except UnsafeUrl as exc:
        return {"error": f"That URL can't be used for a connector: {exc}"}
    existing = await find_existing(ctx.db, ctx.user.id, url)
    return await add_server(ctx, url, str((args or {}).get("name") or ""), existing)


def _card(ctx, row, *, redirect_url: str = "", needs_key: bool = False, description: str = "") -> dict:
    agent = getattr(ctx, "agent", None)
    data = {
        "connector_id": str(row.id),
        "toolkit": "",
        "name": row.alias or row.name,
        "logo": "",
        "description": description,
        "redirect_url": redirect_url,
        "needs_credentials": needs_key,
        "fields": [{"name": "token", "label": "API key or token", "secret": True}] if needs_key else [],
        "server_url": row.server_url,
        "agent_id": str(getattr(agent, "id", "") or ""),
        "agent_name": str(getattr(agent, "name", "") or ""),
    }
    # The chat turns rows with a redirect_url or needs_credentials into connect cards (deep._connect_cards).
    ctx.results.append(
        {
            "result_id": f"r{len(ctx.results) + 1}",
            "source_tool": TOOL,
            "connector_slug": CONNECTORS_SLUG,
            "tool_name": TOOL,
            "args": {"url": row.server_url},
            "data": data,
        }
    )
    return data


def _connected(row, count: int | None, names: list[str]) -> dict:
    label = row.alias or row.name
    return {
        "status": "connected",
        "connector": label,
        "slug": row.slug,
        "tool_count": count,
        "tools": [n for n in names if n][:20],
        "message": f"Connected {label}" + (f" with {count} tools." if count is not None else "."),
    }


async def add_server(ctx, url: str, name: str, existing) -> dict:
    """Connect, then save the connector with its tools, or leave it pending behind a sign-in or key card."""
    db = ctx.db
    probe_error: BaseException | None = None
    if existing is not None and has_credentials(existing):
        # Added before with a key or a finished sign-in: refresh it with what it has, and only ask again if refused.
        try:
            await resync(db, existing)
            ctx.connectors[existing.slug] = existing
            return _connected(existing, await tool_count(db, existing), [])
        except Exception as exc:  # noqa: BLE001
            probe_error = exc
            if not needs_auth(exc):
                return {"error": f"{existing.alias or existing.name} is added but its server isn't answering: {_short(exc)}"}
    try:
        tools = await probe_tools(url, {})
    except Exception as exc:  # noqa: BLE001
        tools, probe_error = None, exc
    if tools is not None:
        row = await save_row(db, ctx.user, url, name, existing, "none", None)
        await index_tools(db, row, tools)
        ctx.connectors[row.slug] = row
        names = [str(getattr(t, "name", "") or (t.get("name") if isinstance(t, dict) else "")) for t in tools]
        return _connected(row, len(tools), names)
    try:
        meta = await discover_auth(url)
    except Exception as exc:  # noqa: BLE001
        log.info("mcp auth discovery failed for %s: %s", url, exc)
        meta = {}
    oauth = bool(meta.get("authorization_endpoint") and meta.get("token_endpoint"))
    if not oauth and not needs_auth(probe_error):
        return {"error": f"Could not connect to an MCP server at {url}: {_short(probe_error) if probe_error else 'no answer'}. Check the URL."}
    if oauth:
        client_id = client_secret = None
        if meta.get("registration_endpoint"):
            try:
                reg = await register_client(meta["registration_endpoint"])
                client_id, client_secret = reg.get("client_id"), reg.get("client_secret")
            except Exception as exc:  # noqa: BLE001
                log.info("mcp client registration failed for %s: %s", url, exc)
        if client_id:
            row = await save_row(db, ctx.user, url, name, existing, "oauth",
                                 {"pending": True, "client_id": client_id, "client_secret": client_secret, "meta": meta})  # fmt: skip
            card = _card(ctx, row, redirect_url=f"/api/connectors/{row.id}/oauth/start",
                         description="This server asks you to sign in. Connect to authorize Lumio.")  # fmt: skip
            return {
                "status": "needs_sign_in",
                "connector": card["name"],
                "slug": row.slug,
                "message": "The server needs sign-in. A Connect card is in the chat: the user clicks it to authorize, "
                "and the connector's tools load once they finish. Tell them that; never ask for a password in chat.",
            }
    row = await save_row(db, ctx.user, url, name, existing, "bearer", None)
    card = _card(ctx, row, needs_key=True,
                 description="This server needs an API key or token. Paste it here; it is stored encrypted and never shown in chat.")  # fmt: skip
    return {
        "status": "needs_api_key",
        "connector": card["name"],
        "slug": row.slug,
        "message": "The server needs an API key. A card in the chat has a key field: ask the user to paste the key "
        "there, never into the chat message.",
    }

