from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.confirm import sign_confirm
from app.agent.invoke import bump_usage, confirmed, invoke_named, preview
from app.agent.memory import load_memory, remember
from app.agent.staff import STAFF_TOOLS, dispatch as staff_dispatch
from app.auth.tokens import ReauthRequired
from app.config import get_settings
from app.connectors.registry import MAIL_CORE_TOOLS
from app.connectors.runtime import RuntimeConnector
from app.models import Connector, ServerCard
from app.routing.describe import catalog_lines
from app.connectors.shape import slim_search
from app.routing.retrieve import (
    GATEWAY_EXECUTE_NAMES,
    GATEWAY_RESOLVE_NAMES,
    GATEWAY_SEARCH_NAMES,
    capability_query,
    capability_terms,
    gateway_search_query,
    is_catalog_tool,
    nested_tool_rows,
    resolve_call_args,
    search_call_args,
    wrap_through_execute,
)
from app.routing.select import load_named, search_tools, user_connectors
from connector_core import ConnectorAuthError, MAIL_CALENDAR_TOOLS, ToolError
from connector_core.text import clip

log = logging.getLogger(__name__)

MAX_TOOL_CHARS = 24000
INSPECT_CHARS = 32000
MAIL_DETAIL_TOOLS = {"get_thread", "get_message"}
CATALOG_TOOLS = {
    "gateway__tool_search",
    "gateway__resolve",
    "gateway__list_mcps",
    "find_tools",
    "load_group",
}


@dataclass
class RunContext:
    db: AsyncSession | None
    user: Any
    connectors: dict[str, Connector]
    confirmations: list[dict] = field(default_factory=list)
    auto_confirm: bool = False
    attachments: list = field(default_factory=list)
    used_tools: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    pending: list[dict] = field(default_factory=list)
    query: str = ""
    call_impl: Any = None
    results: list[dict] = field(default_factory=list)
    gateway_surface: dict[str, dict] = field(default_factory=dict)


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, default=str))
    except TypeError:
        return len(str(value))


def _item_keys(items: list) -> list[str]:
    seen: list[str] = []
    for item in items[:8]:
        if not isinstance(item, dict):
            continue
        for key in item:
            if key not in seen:
                seen.append(key)
    return seen


def outline(value: Any, depth: int = 0) -> Any:
    if isinstance(value, dict):
        if depth >= 4:
            return {"type": "object", "keys": list(value.keys())[:60], "chars": _json_size(value)}
        return {key: outline(item, depth + 1) for key, item in list(value.items())[:80]}
    if isinstance(value, list):
        return {"type": "list", "count": len(value), "item_keys": _item_keys(value)}
    if isinstance(value, str) and len(value) > 160:
        return {"type": "string", "chars": len(value)}
    return value


def compact_payload(value: Any, limit: int = MAX_TOOL_CHARS) -> Any:
    if _json_size(value) <= limit:
        return value
    return {
        "too_large": True,
        "chars": _json_size(value),
        "map": outline(value),
        "hint": (
            "The full result is stored. Call inspect_result with this result_id and a path or search "
            "taken from the user's ask. Page with offset until every requested row is retrieved."
        ),
    }


def dumps(value: Any, limit: int = MAX_TOOL_CHARS) -> str:
    return json.dumps(compact_payload(value, limit), default=str)


def dumps_full(value: Any) -> str:
    return json.dumps(value, default=str)


def _path_parts(path: str) -> list[str]:
    return [part for part in path.replace("/", ".").split(".") if part]


def walk_path(data: Any, path: str) -> tuple[Any, list[str], Any]:
    current = data
    used: list[str] = []
    for part in _path_parts(path):
        if isinstance(current, dict):
            key = current.get(part)
            if part not in current:
                match = next((name for name in current if name.lower() == part.lower()), None)
                if match is None:
                    return None, used, list(current.keys())[:50]
                current = current[match]
            else:
                current = key
            used.append(part)
            continue
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
            used.append(part)
            continue
        return None, used, {"list_count": len(current)} if isinstance(current, list) else None
    return current, used, None


def search_nodes(data: Any, needle: str, prefix: str = "", found: list | None = None, cap: int = 24) -> list[dict]:
    found = found if found is not None else []
    if len(found) >= cap or needle == "":
        return found
    text = needle.lower()
    if isinstance(data, dict):
        for key, item in data.items():
            path = f"{prefix}.{key}" if prefix else key
            if text in key.lower():
                found.append({"path": path, "value": item})
            else:
                search_nodes(item, needle, path, found, cap)
            if len(found) >= cap:
                break
    elif isinstance(data, list):
        for index, item in enumerate(data[:120]):
            search_nodes(item, needle, f"{prefix}.{index}" if prefix else str(index), found, cap)
            if len(found) >= cap:
                break
    return found


def page_value(value: Any, offset: int = 0, limit: int = 80) -> Any:
    if isinstance(value, list):
        nxt = offset + limit
        return {
            "type": "list",
            "total": len(value),
            "offset": offset,
            "items": value[offset:nxt],
            "next_offset": nxt if nxt < len(value) else None,
        }
    if isinstance(value, dict):
        keys = list(value.keys())
        nxt = offset + limit
        return {
            "type": "object",
            "total_keys": len(keys),
            "offset": offset,
            "items": {key: value[key] for key in keys[offset:nxt]},
            "next_offset": nxt if nxt < len(keys) else None,
        }
    return value


def inspect_user_result(
    ctx: RunContext,
    result_id: str | None = None,
    path: str | None = None,
    search: str | None = None,
    offset: int = 0,
    limit: int = 80,
) -> dict:
    if not ctx.results:
        return {"error": "no stored tool result yet — call_tool first"}
    row = ctx.results[-1]
    if result_id:
        row = next((item for item in ctx.results if item.get("result_id") == result_id), None) or row
    data = row.get("data")
    picked = data
    missing = None
    if path:
        picked, used, missing = walk_path(data, path)
        if picked is None:
            return {"error": "path not found", "used": used, "available": missing, "result_id": row.get("result_id")}
    elif search:
        hits = search_nodes(data, search)
        if not hits:
            return {"error": "no keys matched that search", "result_id": row.get("result_id"), "map": outline(data)}
        if len(hits) == 1:
            picked = hits[0]["value"]
            path = hits[0]["path"]
        else:
            return {
                "result_id": row.get("result_id"),
                "matches": [{"path": hit["path"], "map": outline(hit["value"])} for hit in hits],
                "hint": "Call inspect_result again with one of these paths.",
            }
    paged = page_value(picked, offset, max(1, min(limit, 200)))
    if _json_size(paged) > INSPECT_CHARS:
        return {
            "result_id": row.get("result_id"),
            "path": path,
            "too_large": True,
            "map": outline(picked),
            "hint": "Narrow the path or raise offset in smaller pages.",
        }
    return {"result_id": row.get("result_id"), "path": path, "data": paged}


def as_dict(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def _now(tz: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(tz or "Asia/Kolkata"))
    except Exception:  # noqa: BLE001
        return datetime.now(ZoneInfo("Asia/Kolkata"))


def _mail_runtime(ctx: RunContext) -> RuntimeConnector | None:
    for conn in ctx.connectors.values():
        if conn.provider in {"gmail", "outlook"}:
            return RuntimeConnector(conn)
    return None


def _source_tool(slug: str, tool_name: str, args: dict) -> str:
    inner = args.get("tool_name") if tool_name in {"gateway__execute", "execute"} else None
    if inner:
        return f"{slug}__{inner}"
    return f"{slug}__{tool_name}"


def _rewrite_catalog_search_args(tool_name: str, args: dict, fallback_query: str) -> dict:
    if not is_catalog_tool(tool_name) or "search" not in tool_name.lower():
        return args
    key = next((k for k in ("query", "q", "search", "text") if k in args), "query")
    raw = args.get(key) or fallback_query
    return {**args, key: gateway_search_query(raw)}


def _schema_from_resolve(slug: str, tool_name: str, result: Any) -> dict | None:
    if not isinstance(result, dict):
        return None
    item = result.get("tool") or result.get("resolved") or result.get("candidate") or result
    if not isinstance(item, dict):
        return None
    nested = item.get("tool") if isinstance(item.get("tool"), dict) else item
    schema = (
        nested.get("input_schema")
        or nested.get("inputSchema")
        or nested.get("parameters")
        or nested.get("arguments")
        or {}
    )
    if not isinstance(schema, dict):
        schema = {"type": "object", "properties": {}}
    if schema.get("properties") is None and any(k in nested for k in ("type", "properties")):
        schema = {k: nested[k] for k in nested if k in {"type", "properties", "required", "additionalProperties"}}
    name = nested.get("public_name") or nested.get("name") or nested.get("tool_name") or tool_name
    desc = nested.get("description") or item.get("description") or ""
    if not name:
        return None
    return {
        "connector_slug": slug,
        "tool_name": name,
        "description": desc,
        "parameters": schema if schema.get("type") or schema.get("properties") else {"type": "object", "additionalProperties": True},
        "read_only": True,
        "destructive": False,
    }


async def _silent_call(ctx: RunContext, conn, tool_name: str, args: dict) -> Any:
    if ctx.call_impl is not None:
        return await ctx.call_impl(conn.slug, tool_name, args)
    return await invoke_named(ctx.db, ctx.user, conn, tool_name, args, ctx.attachments)


async def _load_gateway_surface(ctx: RunContext, conn) -> dict:
    cached = ctx.gateway_surface.get(conn.slug)
    if cached:
        return cached
    surface = {"search": None, "search_schema": {}, "execute": None, "resolve": None, "resolve_schema": {}}
    if ctx.db is None:
        ctx.gateway_surface[conn.slug] = surface
        return surface
    wanted = {(conn.slug, name) for name in (*GATEWAY_SEARCH_NAMES, *GATEWAY_EXECUTE_NAMES, *GATEWAY_RESOLVE_NAMES)}
    found = await load_named(ctx.db, [conn], wanted)
    by_name = {hit.tool_name: hit for hit in found}
    for name in GATEWAY_SEARCH_NAMES:
        if name in by_name:
            surface["search"] = name
            surface["search_schema"] = by_name[name].schema or {}
            break
    for name in GATEWAY_EXECUTE_NAMES:
        if name in by_name:
            surface["execute"] = name
            break
    for name in GATEWAY_RESOLVE_NAMES:
        if name in by_name:
            surface["resolve"] = name
            surface["resolve_schema"] = by_name[name].schema or {}
            break
    ctx.gateway_surface[conn.slug] = surface
    return surface


async def _expand_gateway_catalog(ctx: RunContext, conn, query: str) -> list[dict]:
    if getattr(conn, "kind", None) == "builtin" or getattr(conn, "provider", None) in {"gmail", "outlook"}:
        return []
    surface = await _load_gateway_surface(ctx, conn)
    search_name = surface.get("search")
    execute_name = surface.get("execute")
    if not search_name or not execute_name:
        return []
    queries: list[str] = []
    for candidate in (gateway_search_query(query), capability_query(query)):
        if candidate and candidate not in queries:
            queries.append(candidate)
    parsed: list[dict] = []
    for q in queries:
        try:
            raw = await _silent_call(ctx, conn, search_name, search_call_args(surface.get("search_schema"), q))
        except Exception:  # noqa: BLE001
            log.warning("gateway catalog search failed on %s", conn.slug, exc_info=True)
            continue
        slim = slim_search(raw, q) if isinstance(raw, dict) else {"candidates": []}
        parsed = [item for item in (slim.get("candidates") or []) if isinstance(item, dict)]
        if parsed:
            break
    rows = nested_tool_rows(
        conn.slug,
        conn.alias or conn.name,
        parsed,
        execute_name,
        capability_terms(query),
    )
    return rows


_MAIL_OPEN_HINTS = (
    "open",
    "summarize",
    "summary",
    "attachment",
    "pdf",
    "display",
    "show",
    "preview",
    "thread",
    "conversation",
    "email",
    "mail",
    "inbox",
    "briefing",
    "read",
)


def _wants_mail_open(query: str) -> bool:
    q = (query or "").lower()
    return any(hint in q for hint in _MAIL_OPEN_HINTS)


def _first_mail_ref(result: Any) -> tuple[str, str]:
    if not isinstance(result, dict):
        return "", ""
    msgs = result.get("messages") or result.get("threads") or []
    hit = msgs[0] if msgs and isinstance(msgs[0], dict) else result
    if not isinstance(hit, dict):
        return "", ""
    tid = str(hit.get("thread_id") or result.get("thread_id") or "").strip()
    mid = str(hit.get("message_id") or hit.get("last_message_id") or hit.get("id") or result.get("message_id") or "").strip()
    return tid, mid


def _complete_thread_args(ctx: RunContext, args: dict) -> dict:
    out = dict(args)
    tid = str(out.get("thread_id") or "").strip()
    mid = str(out.get("message_id") or "").strip()
    if tid and mid:
        return out
    for row in reversed(ctx.results):
        data = row.get("data")
        if not isinstance(data, dict):
            continue
        opened = data.get("opened_thread") if isinstance(data.get("opened_thread"), dict) else None
        msg = data.get("opened_message") if isinstance(data.get("opened_message"), dict) else None
        hit = opened or msg
        if hit is None:
            msgs = data.get("messages") or data.get("threads") or []
            hit = msgs[0] if msgs and isinstance(msgs[0], dict) else data
        if not isinstance(hit, dict):
            continue
        if not tid:
            tid = str(hit.get("thread_id") or "").strip()
        if not mid:
            mid = str(hit.get("message_id") or hit.get("last_message_id") or "").strip()
        if tid or mid:
            break
    if tid:
        out["thread_id"] = tid
    if mid:
        out["message_id"] = mid
    return out


def _mail_threads(ctx: RunContext) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for row in ctx.results:
        tool = str(row.get("source_tool") or "")
        args = row.get("args") if isinstance(row.get("args"), dict) else {}
        data = row.get("data")
        if not isinstance(data, dict):
            continue
        slug = str(row.get("connector_slug") or "")
        if not slug and "__" in tool:
            slug = tool.split("__", 1)[0]
        opened = data.get("opened_thread") if isinstance(data.get("opened_thread"), dict) else None
        msg = data.get("opened_message") if isinstance(data.get("opened_message"), dict) else None
        hit = opened or msg or data
        if not isinstance(hit, dict):
            continue
        mailish = any(name in tool for name in ("get_thread", "get_message", "search_messages", "list_messages", "list_threads", "summarize_attachments"))
        mailish = mailish or bool(hit.get("thread_id") or hit.get("messages") or hit.get("last_message_id"))
        if not mailish:
            continue
        tid = str(hit.get("thread_id") or args.get("thread_id") or "").strip()
        mid = str(hit.get("message_id") or hit.get("last_message_id") or args.get("message_id") or "").strip()
        msgs = hit.get("messages") if isinstance(hit.get("messages"), list) else []
        if not mid and msgs and isinstance(msgs[-1], dict):
            mid = str(msgs[-1].get("message_id") or "").strip()
        key = (slug, tid, mid)
        if (not tid and not mid) or key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "connector_slug": slug,
                "thread_id": tid,
                "message_id": mid,
                "subject": hit.get("subject") or "",
            }
        )
    return out


async def _invoke_follow(ctx: RunContext, slug: str, tool: str, args: dict) -> Any:
    conn = ctx.connectors[slug]
    if ctx.call_impl is not None:
        return await ctx.call_impl(slug, tool, args)
    return await invoke_named(ctx.db, ctx.user, conn, tool, args, ctx.attachments)


async def _hydrate_opened_mail(ctx: RunContext, slug: str, tool_name: str, result: Any) -> Any:
    if tool_name in {"get_thread", "summarize_attachments"}:
        return result
    if tool_name not in {"search_messages", "list_messages", "list_threads", "get_message"}:
        return result
    if not isinstance(result, dict) or result.get("error"):
        return result
    if tool_name != "get_message" and not _wants_mail_open(ctx.query):
        return result
    tid, mid = _first_mail_ref(result)
    if not tid and not mid:
        return result
    thread_args = {"thread_id": tid, "message_id": mid, "max_messages": 50}
    thread = await _invoke_follow(ctx, slug, "get_thread", thread_args)
    thread_name = f"{slug}__get_thread"
    if thread_name not in ctx.used_tools:
        ctx.used_tools.append(thread_name)
    out = dict(result)
    out["opened_thread"] = thread if isinstance(thread, dict) else {"result": thread}
    if isinstance(thread, dict):
        tid = str(thread.get("thread_id") or tid).strip()
        mid = str(thread.get("last_message_id") or mid).strip()
        out["thread_id"] = tid
        if mid:
            out["message_id"] = mid
    latest = None
    if isinstance(thread, dict):
        msgs = thread.get("messages") or []
        if msgs and isinstance(msgs[-1], dict):
            latest = msgs[-1]
    has_atts = bool(latest and (latest.get("attachments") or latest.get("has_attachments")))
    if not has_atts and isinstance(result, dict):
        has_atts = bool(result.get("attachments") or result.get("has_attachments"))
    if has_atts and mid:
        summarized = await _invoke_follow(ctx, slug, "summarize_attachments", {"message_id": mid})
        sum_name = f"{slug}__summarize_attachments"
        if sum_name not in ctx.used_tools:
            ctx.used_tools.append(sum_name)
        out["attachment_open"] = summarized
    out["hint"] = (
        "Opened the full conversation with get_thread (every message, oldest-first). "
        "Cite thread_id from this result. If attachments exist they were summarized. "
        "Do not say this connector is search-only."
    )
    return out


async def list_user_connectors(ctx: RunContext) -> list[dict]:
    cards: dict[str, ServerCard] = {}
    keys = [c.server_key for c in ctx.connectors.values() if getattr(c, "server_key", None)]
    if keys and ctx.db is not None:
        rows = (await ctx.db.execute(select(ServerCard).where(ServerCard.server_key.in_(keys)))).scalars().all()
        cards = {row.server_key: row for row in rows}
    out = []
    for conn in ctx.connectors.values():
        card = cards.get(conn.server_key or "")
        core = list(card.core_tools or []) if card else []
        if conn.provider in {"gmail", "outlook"}:
            core = list(dict.fromkeys([*MAIL_CORE_TOOLS, *core]))
        out.append(
            {
                "slug": conn.slug,
                "name": conn.alias or conn.name,
                "kind": "mail+calendar" if conn.kind == "builtin" else "mcp",
                "description": getattr(conn, "description", "") or "",
                "core_tools": core,
                "tool_count": card.tool_count if card else None,
            }
        )
    return out


async def search_user_tools(ctx: RunContext, query: str, connector_slug: str | None = None) -> list[dict]:
    conns = list(ctx.connectors.values())
    if connector_slug:
        conn = ctx.connectors.get(connector_slug)
        if conn is None:
            return [{"error": f"connector '{connector_slug}' is not connected for this user"}]
        conns = [conn]
    hits = await search_tools(ctx.db, conns, query, 40) if ctx.db is not None else []
    q = (query or "").lower()
    wanted: set[tuple[str, str]] = set()
    mail_conns = [c for c in conns if c.provider in {"gmail", "outlook"}]
    calendary = any(k in q for k in ("invite", "meeting", "calendar", "event", "google meet", "teams", "free slot"))
    if calendary:
        for c in mail_conns:
            wanted.update((c.slug, n) for n in ("create_event", "create_google_meet", "create_teams_meeting", "find_free_slots", "list_events"))
    if any(k in q for k in ("send", "email", "compose", "mail to", "write", "draft")) and not calendary:
        for c in mail_conns:
            wanted.update((c.slug, n) for n in ("send_message", "create_draft", "reply", "forward"))
    if any(k in q for k in ("reply", "respond")):
        for c in mail_conns:
            wanted.add((c.slug, "reply"))
    if "forward" in q:
        for c in mail_conns:
            wanted.add((c.slug, "forward"))
    if any(k in q for k in ("open", "attach", "pdf", "summarize", "thread", "inbox", "email", "mail", "briefing")):
        for c in mail_conns:
            wanted.update((c.slug, n) for n in ("get_thread", "get_message", "list_attachments", "summarize_attachments"))
    if any(k in q for k in ("spam", "junk", "trash", "drafts", "drafted", "delete all", "empty", "clear all", "bulk")):
        for c in mail_conns:
            wanted.update((c.slug, n) for n in ("empty_folder", "bulk_delete", "list_drafts", "delete_draft"))
    if wanted and ctx.db is not None:
        extra = await load_named(ctx.db, mail_conns, wanted)
        hits = extra + [h for h in hits if (h.slug, h.tool_name) not in {(e.slug, e.tool_name) for e in extra}]
    local = [
        {
            "connector_slug": hit.slug,
            "connector_name": hit.connector_name,
            "tool_name": hit.tool_name,
            "description": hit.description,
            "group": hit.group,
            "produces": hit.produces,
            "read_only": hit.read_only,
            "destructive": hit.destructive,
        }
        for hit in hits
    ]
    nested: list[dict] = []
    for conn in conns:
        already = [
            row
            for row in local
            if row["connector_slug"] == conn.slug
            and not is_catalog_tool(row["tool_name"])
            and row["tool_name"] not in GATEWAY_EXECUTE_NAMES
        ]
        if already:
            continue
        nested.extend(await _expand_gateway_catalog(ctx, conn, query))
    nested_keys = {(row["connector_slug"], row["tool_name"]) for row in nested}
    data_local = [
        row
        for row in local
        if not is_catalog_tool(row["tool_name"])
        and row["tool_name"] not in GATEWAY_EXECUTE_NAMES
        and (row["connector_slug"], row["tool_name"]) not in nested_keys
    ]
    catalog_local = [
        row
        for row in local
        if is_catalog_tool(row["tool_name"]) or row["tool_name"] in GATEWAY_EXECUTE_NAMES
    ]
    merged = nested + data_local + (catalog_local if not nested else [])
    # #region agent log
    from app.debug_ndjson import dbg

    dbg(
        "D",
        "deep.py:search_user_tools",
        "catalog search results",
        {
            "query": (query or "")[:80],
            "slug": connector_slug or "",
            "tools": [f"{row['connector_slug']}.{row['tool_name']}" for row in merged],
            "nested": len(nested),
            "has_get_thread": any(row["tool_name"] == "get_thread" for row in merged),
            "has_get_message": any(row["tool_name"] == "get_message" for row in merged),
            "has_send_message": any(row["tool_name"] == "send_message" for row in merged),
            "has_summarize_attachments": any(row["tool_name"] == "summarize_attachments" for row in merged),
            "has_list_attachments": any(row["tool_name"] == "list_attachments" for row in merged),
        },
    )
    # #endregion
    return [{k: v for k, v in row.items() if k != "score"} for row in merged]


def _builtin_mail_spec(conn, tool_name: str) -> dict | None:
    if conn.provider not in {"gmail", "outlook"}:
        return None
    spec = next((s for s in MAIL_CALENDAR_TOOLS if s.name == tool_name), None)
    if spec is None:
        return None
    return {
        "connector_slug": conn.slug,
        "tool_name": spec.name,
        "description": spec.description,
        "parameters": spec.input_schema or {"type": "object", "properties": {}},
        "read_only": spec.read_only,
        "destructive": spec.destructive,
    }


async def get_user_tool_schema(ctx: RunContext, connector_slug: str, tool_name: str) -> dict:
    conn = ctx.connectors.get(connector_slug)
    if conn is None:
        return {"error": f"connector '{connector_slug}' is not connected for this user"}
    hits = await load_named(ctx.db, [conn], {(connector_slug, tool_name)}) if ctx.db is not None else []
    if not hits:
        fallback = _builtin_mail_spec(conn, tool_name)
        # #region agent log
        from app.debug_ndjson import dbg

        dbg(
            "A",
            "deep.py:get_user_tool_schema",
            "tool missing from index",
            {"slug": connector_slug, "tool": tool_name, "server_key": bool(conn.server_key), "fallback": bool(fallback)},
        )
        # #endregion
        if fallback:
            return fallback
        surface = await _load_gateway_surface(ctx, conn)
        execute = surface.get("execute")
        if execute and tool_name != execute and not is_catalog_tool(tool_name):
            resolve = surface.get("resolve")
            if resolve:
                try:
                    raw = await _silent_call(
                        ctx, conn, resolve, resolve_call_args(surface.get("resolve_schema"), tool_name)
                    )
                    shaped = _schema_from_resolve(connector_slug, tool_name, raw)
                    if shaped:
                        shaped["execute_via"] = execute
                        return shaped
                except Exception:  # noqa: BLE001
                    log.warning("gateway resolve failed on %s %s", connector_slug, tool_name, exc_info=True)
            return {
                "connector_slug": connector_slug,
                "tool_name": tool_name,
                "description": (
                    f"Data tool on {conn.alias or conn.name}. Call call_tool with this tool_name; "
                    f"it runs through {execute}."
                ),
                "parameters": {"type": "object", "additionalProperties": True},
                "execute_via": execute,
                "read_only": True,
                "destructive": False,
            }
        return {"error": f"unknown tool {tool_name} on {connector_slug}. search_tools first."}
    hit = hits[0]
    return {
        "connector_slug": hit.slug,
        "tool_name": hit.tool_name,
        "description": hit.description,
        "parameters": hit.schema or {"type": "object", "properties": {}},
        "read_only": hit.read_only,
        "destructive": hit.destructive,
    }


async def call_user_tool(ctx: RunContext, connector_slug: str, tool_name: str, args: dict | None = None) -> Any:
    args = as_dict(args)
    conn = ctx.connectors.get(connector_slug)
    if conn is None:
        return {"error": f"connector '{connector_slug}' is not connected for this user"}
    qualified = f"{connector_slug}__{tool_name}"
    ctx.used_tools.append(qualified)
    hit = None
    if ctx.db is not None:
        recovered = await load_named(ctx.db, [conn], {(connector_slug, tool_name)})
        hit = recovered[0] if recovered else None
    spec = next((s for s in MAIL_CALENDAR_TOOLS if s.name == tool_name), None)
    if hit is None and spec is None:
        surface = await _load_gateway_surface(ctx, conn)
        execute = surface.get("execute")
        tool_name, args = wrap_through_execute(tool_name, args, execute)
    args = _rewrite_catalog_search_args(tool_name, args, ctx.query)
    destructive = bool(hit.destructive if hit is not None else (spec.destructive if spec else False))
    if destructive and not ctx.auto_confirm and not confirmed(ctx.confirmations, connector_slug, tool_name):
        token = sign_confirm("tool", {"slug": connector_slug, "tool": tool_name, "args": args})
        pending = {
            "needs_confirm": True,
            "token": token,
            "title": f"{(hit.connector_name if hit else conn.alias or conn.name)}: {tool_name}",
            "preview": await preview(ctx.user, conn, tool_name, args),
        }
        ctx.pending.append(pending)
        return {"status": "awaiting_user_confirmation", "preview": pending["preview"]}
    if tool_name == "get_thread":
        args = _complete_thread_args(ctx, args)
    try:
        if ctx.call_impl is not None:
            result = await ctx.call_impl(connector_slug, tool_name, args)
        else:
            result = await invoke_named(ctx.db, ctx.user, conn, tool_name, args, ctx.attachments)
        # #region agent log
        from app.debug_ndjson import dbg

        payload_preview = result if isinstance(result, dict) else {"type": type(result).__name__}
        dbg(
            "C",
            "deep.py:call_user_tool",
            "tool invoked",
            {
                "slug": connector_slug,
                "tool": tool_name,
                "has_error": bool(isinstance(result, dict) and result.get("error")),
                "error": (result.get("error")[:160] if isinstance(result, dict) and result.get("error") else ""),
                "has_message_id": bool(isinstance(result, dict) and (result.get("message_id") or (result.get("messages") or [{}])[0].get("message_id"))),
                "attachment_count": len(result.get("attachments") or []) if isinstance(result, dict) else 0,
                "keys": list(payload_preview.keys())[:12] if isinstance(payload_preview, dict) else [],
            },
        )
        # #endregion
        result = await _hydrate_opened_mail(ctx, connector_slug, tool_name, result)
        # #region agent log
        if tool_name in {"search_messages", "list_messages"}:
            from app.debug_ndjson import dbg

            dbg(
                "B",
                "deep.py:_hydrate_opened_mail",
                "mail follow-up",
                {
                    "slug": connector_slug,
                    "wants_open": _wants_mail_open(ctx.query),
                    "did_open": bool(isinstance(result, dict) and result.get("opened_message")),
                    "did_summarize": bool(isinstance(result, dict) and result.get("attachment_open")),
                },
            )
        # #endregion
        if ctx.db is not None:
            await bump_usage(ctx.db, ctx.user.id, conn.id, tool_name, True)
        source_name = _source_tool(connector_slug, tool_name, args)
        rid = f"r{len(ctx.results) + 1}"
        ctx.results.append(
            {
                "result_id": rid,
                "source_tool": source_name,
                "connector_slug": connector_slug,
                "tool_name": tool_name,
                "args": args,
                "data": result,
            }
        )
        payload = compact_payload(result, 60000 if tool_name in MAIL_DETAIL_TOOLS else MAX_TOOL_CHARS)
        if isinstance(payload, dict) and payload.get("too_large"):
            payload["result_id"] = rid
        if tool_name not in CATALOG_TOOLS:
            if source_name not in ctx.used_tools:
                ctx.used_tools.append(source_name)
            ctx.sources.append(
                {
                    "source_tool": source_name,
                    "connector_slug": connector_slug,
                    "tool_name": tool_name,
                    "args": args,
                    "result_id": rid,
                    "snippet": compact_payload(payload, 2000),
                }
            )
        return payload
    except (ConnectorAuthError, ReauthRequired) as exc:
        conn.health = "auth_expired"
        conn.health_detail = str(exc)[:500]
        if ctx.db is not None:
            try:
                await ctx.db.flush()
            except Exception:  # noqa: BLE001
                log.warning("could not persist auth expiry")
            await bump_usage(ctx.db, ctx.user.id, conn.id, tool_name, False)
        return {"error": "This account needs to be reconnected.", "reconnect": connector_slug}
    except ToolError as exc:
        if ctx.db is not None:
            await bump_usage(ctx.db, ctx.user.id, conn.id, tool_name, False)
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        log.exception("tool failed")
        if ctx.db is not None:
            await bump_usage(ctx.db, ctx.user.id, conn.id, tool_name, False)
        return {"error": str(exc)[:300]}


async def run_staff(ctx: RunContext, name: str, args: dict | None = None) -> Any:
    if name not in STAFF_TOOLS:
        return {"error": f"unknown staff tool {name}", "available": sorted(STAFF_TOOLS)}
    ctx.used_tools.append(name)
    runtime = _mail_runtime(ctx)
    if runtime is None and name not in {
        "schedule_task",
        "list_scheduled_tasks",
        "update_scheduled_task",
        "create_automation",
        "list_automations",
        "list_commitments",
    }:
        return {"error": "No mail connector is enabled for this user."}
    try:
        result = await staff_dispatch(ctx.db, ctx.user, runtime, name, as_dict(args))
        ctx.sources.append({"source_tool": name, "args": as_dict(args), "snippet": compact_payload(result, 2000)})
        return compact_payload(result)
    except Exception as exc:  # noqa: BLE001
        log.exception("staff tool failed")
        return {"error": str(exc)[:300]}


class SearchToolsInput(BaseModel):
    query: str = Field(description="Natural language description of the capability you need.")
    connector_slug: str | None = Field(default=None, description="Optional slug from list_connectors to search one connector.")


class GetSchemaInput(BaseModel):
    connector_slug: str
    tool_name: str


class CallToolInput(BaseModel):
    connector_slug: str = Field(description="Slug from list_connectors.")
    tool_name: str = Field(description="Exact tool name from search_tools or get_tool_schema.")
    args: dict[str, Any] = Field(
        default_factory=dict,
        description="Arguments from the tool schema. For a gateway execute tool this is usually {tool_name, arguments}.",
    )


class StaffInput(BaseModel):
    name: str = Field(
        description=(
            "today_briefing, ask_inbox, scan_commitments, list_commitments, scan_admin, "
            "schedule_task, list_scheduled_tasks, update_scheduled_task, create_automation, "
            "list_automations, or learn_voice."
        )
    )
    args: dict[str, Any] = Field(default_factory=dict)


class ScheduleTaskInput(BaseModel):
    prompt: str = Field(
        description="The user's full request, including what to do and when (in N minutes, tonight, every day, a clock time)."
    )


class InspectInput(BaseModel):
    result_id: str | None = Field(default=None, description="Stored result_id from call_tool. Defaults to the latest.")
    path: str | None = Field(default=None, description="Dotted path into the stored JSON, e.g. kpis.net_sales or breakdowns.0.rows")
    search: str | None = Field(default=None, description="Find keys whose names match the user's asked slice (style, sku, region, ...).")
    offset: int = Field(default=0, description="Skip this many list items or object keys.")
    limit: int = Field(default=80, description="How many list items or keys to return.")


def harness_tools(ctx: RunContext) -> list[StructuredTool]:
    async def list_connectors() -> str:
        """List this user's enabled connectors. Start here when you are unsure which source to use. Other users have different connectors."""
        return dumps_full(await list_user_connectors(ctx))

    async def search_tools_fn(query: str, connector_slug: str | None = None) -> str:
        """Search this user's tool catalog by capability (what the tool must return). Pass a short phrase, not the full user sentence. Omit brand names; pass connector_slug when known. Gateway catalogs are expanded into data tools. Returns names and descriptions, not data."""
        return dumps_full(await search_user_tools(ctx, query, connector_slug))

    async def get_tool_schema(connector_slug: str, tool_name: str) -> str:
        """Return the description and JSON schema for one tool so you can fill arguments correctly."""
        return dumps_full(await get_user_tool_schema(ctx, connector_slug, tool_name))

    async def call_tool(connector_slug: str, tool_name: str, args: dict[str, Any] | None = None) -> str:
        """Run a tool on one of this user's connectors now. Do not use this to send or fetch if the user named a future time — call schedule_task instead. Catalog/search/resolve/list tools are not answers. Large results are stored; use inspect_result for the slice the user asked for."""
        return dumps(await call_user_tool(ctx, connector_slug, tool_name, args), 60000)

    async def schedule_task(prompt: str) -> str:
        """Queue work for a future time. Required when the user says in N minutes, later, tonight, tomorrow, every day, or any clock. Pass their full request. Do not fetch data or send mail in this turn."""
        return dumps(await run_staff(ctx, "schedule_task", {"prompt": prompt}))

    async def inspect_result(
        result_id: str | None = None,
        path: str | None = None,
        search: str | None = None,
        offset: int = 0,
        limit: int = 80,
    ) -> str:
        """Read a stored tool result. Use path or search from the user's ask, then page with offset until every requested row is in hand. Do not stop because the first call_tool reply was large."""
        return dumps(inspect_user_result(ctx, result_id, path, search, offset, limit), 32000)

    async def run_staff_tool(name: str, args: dict[str, Any] | None = None) -> str:
        """Run an assistant staff capability (briefing, inbox question, commitments, scheduled tasks, automations, voice)."""
        return dumps(await run_staff(ctx, name, args))

    return [
        StructuredTool.from_function(coroutine=list_connectors, name="list_connectors", description=list_connectors.__doc__),
        StructuredTool.from_function(
            coroutine=search_tools_fn,
            name="search_tools",
            description=search_tools_fn.__doc__,
            args_schema=SearchToolsInput,
        ),
        StructuredTool.from_function(
            coroutine=get_tool_schema,
            name="get_tool_schema",
            description=get_tool_schema.__doc__,
            args_schema=GetSchemaInput,
        ),
        StructuredTool.from_function(
            coroutine=call_tool,
            name="call_tool",
            description=call_tool.__doc__,
            args_schema=CallToolInput,
        ),
        StructuredTool.from_function(
            coroutine=inspect_result,
            name="inspect_result",
            description=inspect_result.__doc__,
            args_schema=InspectInput,
        ),
        StructuredTool.from_function(
            coroutine=schedule_task,
            name="schedule_task",
            description=schedule_task.__doc__,
            args_schema=ScheduleTaskInput,
        ),
        StructuredTool.from_function(
            coroutine=run_staff_tool,
            name="run_staff_tool",
            description=run_staff_tool.__doc__,
            args_schema=StaffInput,
        ),
    ]


def system_prompt(user, connectors: list, memory: str) -> str:
    tz = getattr(user, "timezone", None) or "Asia/Kolkata"
    now = _now(tz)
    catalog = catalog_lines(connectors)
    return (
        "You are this user's personal chief of staff. You have mail, calendar, and connected MCP servers.\n"
        f"Current date and time: {now.strftime('%Y-%m-%d %H:%M')} ({tz}). Interpret and display times in that timezone.\n"
        "Only use connectors listed below. Other users have different connectors.\n"
        "Discover first: list_connectors, then search_tools with a short capability phrase (what the tool must return), not the full user sentence. "
        "Omit brand names, people, and dates from the search query; pass those as tool arguments after get_tool_schema. "
        "Once you know the source, pass connector_slug so search is not split across mail and analytics. "
        "Mail and analytics are different connectors — do not treat a missing mail tool as a missing MCP connector.\n"
        "search_tools already expands gateway catalogs into callable data tools. Prefer those names. "
        "Do not call gateway catalog tools from core_tools unless search_tools returned no data tool.\n"
        "Read get_tool_schema before filling arguments. Argument names come from that schema and description.\n"
        "Search, resolve, list_mcps, and gateway__tool_search return catalogs, not answers. After you pick a data tool, call_tool it. "
        "If a data tool returns a snapshot or as-of date, report those figures and the as-of date — that is still the answer. "
        "If the first search is weak, search once more with different capability words, then call_tool. Do not stop on a catalog search.\n"
        "Call tools as many times as the task needs — different connectors, different argument sets, different windows. Combine results yourself.\n"
        "Gmail and Outlook are mail AND calendar. Never say there is no calendar connector when gmail or outlook is listed. "
        "Send mail with send_message. Create a calendar invite with create_event (attendees, start in IST). "
        "Google Meet: create_google_meet on gmail. Teams: create_teams_meeting on outlook. "
        "After search_messages or list_messages, if the user asked to open, read, or summarize mail, call get_thread "
        "with thread_id AND message_id from the hit. Never call get_thread with an empty thread_id — pass message_id instead. "
        "Use every message in the thread, not only the latest reply. Then summarize_attachments if files exist. "
        "list_messages includes mail where the user is on To or Cc (recipient_role). Cover CC'd mail in briefings. "
        "When sending or replying, pass cc and bcc if the user named other people to include. "
        "Never say a mail connector cannot compose, send, invite, attach, or batch-delete. "
        "Empty spam, junk, trash, or drafts with empty_folder (folder=spam|trash|drafts) in one call_tool — do not loop delete_draft.\n"
        "Answer every part of the ask. If a result is too large, inspect_result the stored result_id using a path or search from the user's words, and keep paging until the requested slice is complete. Or call_tool again with narrower schema arguments. Never say data was truncated and stop.\n"
        "Cite source_tool (connector slug and tool name, plus the downstream name if a gateway executed one).\n"
        "If asked how a number was produced, cite tool evidence from this thread or fetch again. Do not invent figures.\n"
        "If you cannot tell which connector fits, ask the user.\n"
        "Destructive actions still need call_tool; the app asks the user to confirm.\n"
        "If the user wants work done later (email a report in N minutes, tonight, every day), schedule_task and do not fetch now. "
        "If they want a calendar invite or meeting at a clock time, that time is the event start: call create_event now. "
        "Tell them the scheduled time only when you actually called schedule_task.\n"
        "Never invent emails, events, ids, or numbers that did not come from a tool result. "
        "When connector data comes back, write a detailed analyst briefing — never a short brief. "
        "Cover what the numbers mean, drivers, comparisons, risks, outliers, and recommended actions, with headings and specifics from the tool result.\n\n"
        f"This user's connectors:\n{catalog}\n\nUser memory:\n{memory or '(none)'}"
    )


def _chat_model():
    """gpt-6-luna function tools + reasoning only work on /v1/responses, not chat completions."""
    from app.llm import reasoning_payload

    s = get_settings()
    effort = (s.openai_reasoning_effort or "").strip() or "none"
    reasoning = reasoning_payload()
    base = {"model": s.openai_model, "api_key": s.openai_api_key}
    attempts = [
        {"use_responses_api": True, "reasoning": reasoning, "verbosity": "medium", "service_tier": "default"},
        {"use_responses_api": True, "reasoning": reasoning, "verbosity": "medium"},
        {"use_responses_api": True, "reasoning": reasoning},
        {"use_responses_api": True, "reasoning_effort": effort},
        {"use_responses_api": True},
    ]
    for extra in attempts:
        try:
            return ChatOpenAI(**base, **extra)
        except TypeError:
            continue
    return ChatOpenAI(**base)


def _todo_middleware():
    try:
        from langchain.agents.middleware import TodoListMiddleware

        return [TodoListMiddleware()]
    except Exception:  # noqa: BLE001
        return []


_SHELL_EXCLUDED = False


def _exclude_shell():
    global _SHELL_EXCLUDED
    if _SHELL_EXCLUDED:
        return
    try:
        from deepagents import HarnessProfile, register_harness_profile

        register_harness_profile("openai", HarnessProfile(excluded_tools=frozenset({"execute"})))
        _SHELL_EXCLUDED = True
    except Exception:  # noqa: BLE001
        log.debug("could not exclude shell execute tool")


def build_agent(ctx: RunContext, prompt: str):
    from deepagents import create_deep_agent

    _exclude_shell()
    kwargs: dict[str, Any] = {
        "model": _chat_model(),
        "tools": harness_tools(ctx),
        "system_prompt": prompt,
    }
    middleware = _todo_middleware()
    if middleware:
        kwargs["middleware"] = middleware
    return create_deep_agent(**kwargs)


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
            elif hasattr(block, "text"):
                parts.append(getattr(block, "text") or "")
        return "\n".join(p for p in parts if p).strip()
    return str(content or "").strip()


def _is_recursion_limit(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    if "recursion" in name:
        return True
    msg = str(exc).lower()
    return "recursion limit" in msg or "recursed" in msg


def last_text(messages: list) -> str:
    for msg in reversed(messages or []):
        role = getattr(msg, "type", None) or getattr(msg, "role", None)
        tool_calls = getattr(msg, "tool_calls", None) or []
        if role in {"ai", "assistant"} and not tool_calls:
            text = _message_text(getattr(msg, "content", ""))
            if text:
                return text
    if messages:
        return _message_text(getattr(messages[-1], "content", "")) or "Done."
    return "Done."


def _lc_messages(history: list[dict], query: str, extra: list | None = None):
    from langchain_core.messages import AIMessage, HumanMessage

    out = []
    for item in history or []:
        role = item.get("role")
        content = item.get("content") or ""
        if role == "user":
            out.append(HumanMessage(content=content))
        elif role == "assistant":
            out.append(AIMessage(content=content))
    for item in extra or []:
        out.append(item)
    out.append(HumanMessage(content=query))
    return out


async def run_deep_agent(
    db: AsyncSession,
    user,
    session_id: uuid.UUID,
    request_id: str,
    history: list[dict],
    query: str,
    confirmations: list[dict] | None = None,
    auto_confirm: bool = False,
) -> dict[str, Any]:
    del request_id
    conns = await user_connectors(db, user.id)
    ctx = RunContext(
        db=db,
        user=user,
        connectors={c.slug: c for c in conns},
        confirmations=list(confirmations or []),
        auto_confirm=auto_confirm,
        query=query,
    )
    memory = await load_memory(db, user, query)
    extra = []
    from langchain_core.messages import HumanMessage

    for payload in ctx.confirmations:
        slug, tool = payload.get("slug"), payload.get("tool")
        if not slug or not tool:
            continue
        result = await call_user_tool(ctx, slug, tool, payload.get("args") or {})
        extra.append(
            HumanMessage(
                content=f"The user confirmed {tool} on {slug}. Result: {clip(dumps(result, 4000), 4000)}. Tell the user what happened."
            )
        )
    agent = build_agent(ctx, system_prompt(user, conns, memory))
    payload = {"messages": _lc_messages(history, query, extra)}
    limit = max(80, get_settings().max_agent_steps * 8)
    text = ""
    for attempt in range(3):
        try:
            result = await agent.ainvoke(
                payload,
                config={
                    "recursion_limit": limit,
                    "configurable": {"thread_id": f"{session_id}:r{attempt}"},
                },
            )
            text = last_text(result.get("messages") if isinstance(result, dict) else [])
            break
        except Exception as exc:  # noqa: BLE001
            if _is_recursion_limit(exc) and attempt < 2:
                limit = min(limit + 40, 200)
                log.warning("recursion limit reached; retry %s/2 with limit=%s", attempt + 1, limit)
                continue
            log.exception("deep agent failed")
            text = f"I could not finish that request: {str(exc)[:240]}"
            break
    source = ctx.sources[-1] if ctx.sources else None
    # #region agent log
    from app.debug_ndjson import dbg

    dbg(
        "B",
        "deep.py:run_deep_agent",
        "agent finished",
        {
            "query": (query or "")[:80],
            "used_tools": ctx.used_tools,
            "attachment_cards": len(ctx.attachments),
            "claimed_search_only": "only provides" in (text or "").lower() or "searching" in (text or "").lower(),
            "text_head": (text or "")[:160],
        },
    )
    # #endregion
    await remember(db, user.id, "episodic", clip(f"Q: {query}\nA: {text}", 400))
    return {
        "text": text or "Done.",
        "pending_confirms": ctx.pending,
        "attachments": ctx.attachments,
        "used_tools": ctx.used_tools,
        "route": "deep",
        "source": source,
        "mail_threads": _mail_threads(ctx),
    }
