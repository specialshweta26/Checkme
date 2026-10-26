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

from app.agents import chat_tools as workspace_tools
from app.agents.service import GROUP_MEMBER_MAX, GROUP_MEMBER_MIN, connector_assignment_map, filter_connectors
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
    extra_capability_terms,
    gateway_search_query,
    is_calendar_query,
    is_catalog_tool,
    keep_connector_scope,
    matching_toolkit,
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
    "list_apps",
    "list_connections",
    "resolve",
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
    agent: Any = None
    group: Any = None
    teammates: list = field(default_factory=list)
    teammate_depth: int = 0
    session_id: uuid.UUID | None = None
    request_id: str = ""
    workspace_changed: bool = False
    progress: Any = None


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


def _dbg_arg_peek(args: dict | None) -> dict:
    raw = args or {}
    inner = raw.get("arguments") if isinstance(raw.get("arguments"), dict) else raw
    if not isinstance(inner, dict):
        inner = {}
    keep = ("brand", "brand_id", "entity", "preset", "since", "until", "start", "end", "query", "q", "trend")
    peek = {k: str(inner.get(k))[:80] for k in keep if inner.get(k) not in (None, "")}
    peek["arg_keys"] = list(inner.keys())[:16]
    if raw.get("tool_name"):
        peek["inner_tool"] = str(raw.get("tool_name"))[:120]
    return peek


def _dbg_result_peek(result: Any) -> dict:
    if not isinstance(result, dict):
        return {"type": type(result).__name__}
    daily = result.get("daily") or result.get("days") or result.get("series")
    rows = daily if isinstance(daily, list) else []

    def _num(row: Any) -> float:
        if not isinstance(row, dict):
            return 0.0
        for key in ("net_sales", "net", "sales", "revenue", "value"):
            val = row.get(key)
            if isinstance(val, dict):
                val = val.get("value") or val.get("amount")
            try:
                return float(val or 0)
            except (TypeError, ValueError):
                continue
        return 0.0

    msgs = result.get("messages") or result.get("items") or []
    return {
        "keys": list(result.keys())[:16],
        "brand": str(result.get("brand") or result.get("brand_id") or "")[:40],
        "window": str(result.get("window") or "")[:80],
        "daily_n": len(rows) if isinstance(daily, list) else (None if daily is None else type(daily).__name__),
        "daily_nonzero": sum(1 for row in rows if _num(row) != 0),
        "cat_empty": not bool(result.get("by_category")),
        "msg_n": len(msgs) if isinstance(msgs, list) else None,
        "too_large": bool(result.get("too_large")),
        "has_error": bool(result.get("error")),
    }


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
    if getattr(conn, "provider", None) in {"gmail", "outlook", "daytona"}:
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
    toolkit = None
    search_schema = surface.get("search_schema") or {}
    if getattr(conn, "provider", None) == "composio" and "toolkit" in (search_schema.get("properties") or {}):
        try:
            linked = await _silent_call(ctx, conn, "list_connections", {})
            conns = linked.get("connections") if isinstance(linked, dict) else []
            toolkit = matching_toolkit(conns if isinstance(conns, list) else [], extra_capability_terms(query))
        except Exception:  # noqa: BLE001
            log.warning("composio list_connections failed on expand", exc_info=True)
    parsed: list[dict] = []
    for q in queries:
        args = search_call_args(search_schema, q)
        if toolkit:
            args["toolkit"] = toolkit
        try:
            raw = await _silent_call(ctx, conn, search_name, args)
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
    # #region agent log
    from app.debug_ndjson import dbg

    dbg(
        "D",
        "deep.py:_expand_gateway_catalog",
        "gateway expand",
        {
            "slug": conn.slug,
            "provider": getattr(conn, "provider", "") or "",
            "search": search_name or "",
            "execute": execute_name or "",
            "query": (query or "")[:80],
            "toolkit": toolkit or "",
            "parsed_n": len(parsed),
            "names": [str(r.get("tool_name") or "")[:80] for r in rows[:10]],
        },
    )
    # #endregion
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


def _browser_views(ctx: RunContext) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for row in ctx.results:
        tool = str(row.get("tool_name") or "")
        if not any(token in tool for token in ("browse", "browser_", "computer_observe", "computer_act")):
            continue
        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        args = row.get("args") if isinstance(row.get("args"), dict) else {}
        inner = args.get("arguments") if isinstance(args.get("arguments"), dict) else args
        shot = data.get("screenshot") if isinstance(data.get("screenshot"), dict) else {}
        url = str(data.get("url") or inner.get("url") or "").strip()
        preview = str(shot.get("preview_url") or data.get("preview_url") or "")
        download = str(shot.get("download_url") or data.get("download_url") or "")
        if not url and not preview and not download:
            continue
        key = preview or url or download
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "url": url,
                "title": data.get("title") or "",
                "preview_url": preview,
                "download_url": download,
                "error": data.get("error") or "",
            }
        )
    return out[-4:]


def _computer_session(ctx: RunContext) -> dict:
    views = _browser_views(ctx)
    live = bool(views) or any(
        any(token in str(name) for token in ("browser_", "browse_", "computer_observe", "computer_act", "handoff"))
        for name in ctx.used_tools
    )
    last = views[-1] if views else {}
    preview = str(last.get("preview_url") or "")
    download = str(last.get("download_url") or "")
    if not preview:
        for card in reversed(ctx.attachments or []):
            if not isinstance(card, dict):
                continue
            kind = str(card.get("kind") or "")
            name = str(card.get("filename") or "").lower()
            if kind in {"image", "screen"} or name.endswith(".png") or name.endswith(".jpg") or name == "page.png":
                preview = str(card.get("preview_url") or "")
                download = str(card.get("download_url") or download)
                live = live or bool(preview)
                break
    handoff = next(
        (p for p in ctx.pending if (p.get("kind") == "handoff" or (p.get("preview") or {}).get("kind") == "handoff")),
        None,
    )
    live = live or bool(handoff)
    return {
        "live": live,
        "url": last.get("url") or "",
        "title": last.get("title") or "",
        "preview_url": preview,
        "download_url": download,
        "handoff": bool(handoff),
        "handoff_reason": ((handoff or {}).get("preview") or {}).get("reason") or ((handoff or {}).get("preview") or {}).get("instruction") or "",
    }


_CONNECT_HINTS = ("connect", "authorize", "sign in", "sign-in", "oauth", "link account")
_CONNECT_STOP = frozenset(
    {"connect", "connecting", "authorize", "authorization", "oauth", "sign", "signin", "link", "account", "app", "apps"}
)


def _is_connect_query(query: str) -> bool:
    q = (query or "").lower()
    return any(k in q for k in _CONNECT_HINTS)


def _pick_connect_toolkit(apps: list, query: str) -> dict | None:
    terms = [t for t in extra_capability_terms(query) if t not in _CONNECT_STOP]
    q = (query or "").lower()
    scored: list[tuple[int, int, dict]] = []
    for app in apps:
        if not isinstance(app, dict):
            continue
        slug = str(app.get("slug") or "").strip().lower()
        name = str(app.get("name") or "").strip().lower()
        if not slug:
            continue
        blob = f"{slug} {name}".replace("-", "_")
        score = 0
        if slug in q or (name and name in q):
            score += 6
        for term in terms:
            t = term.replace("-", "_")
            if t == slug or t == name or t in blob.split() or slug.endswith(f"_{t}") or slug.startswith(f"{t}_"):
                score += 4
            elif t in blob or blob in t:
                score += 2
        if score:
            scored.append((score, len(slug), app))
    if not scored:
        return None
    scored.sort(key=lambda row: (-row[0], row[1]))
    return scored[0][2]


async def _maybe_start_connect(ctx: RunContext, query: str) -> None:
    if not _is_connect_query(query) or _connect_cards(ctx):
        return
    conn = next((c for c in ctx.connectors.values() if getattr(c, "provider", None) == "composio"), None)
    if conn is None:
        return
    terms = [t for t in extra_capability_terms(query) if t not in _CONNECT_STOP]
    listed = await call_user_tool(ctx, conn.slug, "list_apps", {"query": " ".join(terms) or query})
    apps = listed.get("apps") if isinstance(listed, dict) else []
    picked = _pick_connect_toolkit(apps if isinstance(apps, list) else [], query)
    toolkit = str((picked or {}).get("slug") or "").strip()
    if not toolkit:
        return
    await call_user_tool(ctx, conn.slug, "connect_app", {"toolkit": toolkit})


def _connect_cards(ctx: RunContext) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for row in ctx.results:
        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        url = str(data.get("redirect_url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(
            {
                "toolkit": data.get("toolkit") or "",
                "name": data.get("name") or data.get("toolkit") or "Connect app",
                "logo": data.get("logo") or "",
                "description": data.get("description") or "",
                "redirect_url": url,
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
                "kind": (
                    "mail+calendar"
                    if conn.provider in {"gmail", "outlook"}
                    else "computer"
                    if conn.provider == "daytona"
                    else "marketplace"
                    if conn.provider == "composio"
                    else "mcp"
                ),
                "description": getattr(conn, "description", "") or "",
                "core_tools": core,
                "tool_count": card.tool_count if card else None,
            }
        )
    # #region agent log
    from app.debug_ndjson import dbg

    dbg(
        "A",
        "deep.py:list_user_connectors",
        "listed connectors",
        {
            "slugs": [item["slug"] for item in out],
            "kinds": [item["kind"] for item in out],
            "has_composio": any(item["slug"] == "composio" or item["kind"] == "marketplace" for item in out),
        },
    )
    # #endregion
    return out


async def search_user_tools(ctx: RunContext, query: str, connector_slug: str | None = None) -> list[dict]:
    conns = list(ctx.connectors.values())
    scoped = False
    if connector_slug:
        conn = ctx.connectors.get(connector_slug)
        if conn is None:
            return [{"error": f"connector '{connector_slug}' is not connected for this user"}]
        if keep_connector_scope(getattr(conn, "provider", None), query):
            conns = [conn]
            scoped = True
    hits = await search_tools(ctx.db, conns, query, 40) if ctx.db is not None else []
    q = (query or "").lower()
    wanted: set[tuple[str, str]] = set()
    mail_conns = [c for c in conns if c.provider in {"gmail", "outlook"}]
    computer_conns = [c for c in conns if getattr(c, "provider", None) == "daytona"]
    composio_conns = [c for c in conns if getattr(c, "provider", None) == "composio"]
    connecting = _is_connect_query(query)
    calendary = is_calendar_query(query)
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
    if any(
        k in q
        for k in (
            "chart",
            "plot",
            "graph",
            "html report",
            "downloadable",
            "visuali",
            "matplotlib",
            "png",
            "csv",
            "excel",
            "report",
            "dashboard",
            "analytics",
            "analysis",
            "kpi",
            "sales",
            "inventory",
            "metric",
            "table",
        )
    ):
        for c in computer_conns:
            wanted.update((c.slug, n) for n in ("write_analytics_report", "run_code", "write_file", "publish_file", "read_file"))
    if not connecting and any(
        k in q
        for k in (
            "browser",
            "website",
            "web page",
            "browse",
            "scrape",
            "open the site",
            "playwright",
            ".com",
            "http://",
            "https://",
            "www.",
            "news",
            "headlines",
            "current events",
            "fill out",
            "fill the form",
            "login page",
            "captcha",
            "what is going on",
            "what's going on",
            "whats going on",
            "happening now",
            "live update",
            "flight",
            "flights",
            "search the web",
            "look up",
            "look for",
            "google",
            "instagram",
            "facebook",
            "twitter",
            "wikipedia",
            "duckduckgo",
        )
    ):
        for c in computer_conns:
            wanted.update(
                (c.slug, n)
                for n in (
                    "browser_navigate",
                    "browser_snapshot",
                    "browser_act",
                    "computer_observe",
                    "computer_act",
                    "browser_handoff",
                    "web_fetch",
                    "browse_open",
                    "browse_act",
                    "publish_file",
                )
            )
    if connecting:
        for c in composio_conns:
            wanted.update((c.slug, n) for n in ("connect_app", "list_apps", "list_connections"))
    extra_conns = mail_conns + computer_conns + composio_conns
    if wanted and ctx.db is not None and extra_conns:
        extra = await load_named(ctx.db, extra_conns, wanted)
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
        if connecting and getattr(conn, "provider", None) == "composio":
            continue
        nested.extend(await _expand_gateway_catalog(ctx, conn, query))
    nested.sort(key=lambda row: float(row.get("score") or 0), reverse=True)
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
        "B",
        "deep.py:search_user_tools",
        "catalog search results",
        {
            "query": (query or "")[:80],
            "slug": connector_slug or "",
            "scoped": scoped,
            "extras": extra_capability_terms(query)[:8],
            "conn_slugs": [c.slug for c in conns],
            "calendary": calendary,
            "has_composio_conn": any(getattr(c, "provider", None) == "composio" for c in conns),
            "tools": [f"{row['connector_slug']}.{row['tool_name']}" for row in merged][:24],
            "nested": len(nested),
            "teams_hits": [
                f"{row['connector_slug']}.{row['tool_name']}"
                for row in merged
                if "team" in (row.get("tool_name") or "").lower()
                or "chat" in (row.get("tool_name") or "").lower()
            ][:12],
            "composio_hits": [row["tool_name"] for row in merged if row.get("connector_slug") == "composio"][:12],
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
            "F",
            "deep.py:get_user_tool_schema",
            "tool missing from index",
            {
                "slug": connector_slug,
                "tool": tool_name,
                "server_key": bool(conn.server_key),
                "fallback": bool(fallback),
                "provider": getattr(conn, "provider", "") or "",
            },
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


def git_write_gate(conn, slug: str, tool: str, args: dict | None) -> bool:
    from app.connectors.github_app import is_git_write

    text = f"{getattr(conn, 'name', '') or ''} {getattr(conn, 'alias', '') or ''} {getattr(conn, 'server_url', '') or ''}"
    return is_git_write(str(getattr(conn, "provider", "") or ""), slug, tool, args, text)


async def call_user_tool(ctx: RunContext, connector_slug: str, tool_name: str, args: dict | None = None) -> Any:
    args = as_dict(args)
    orig_tool = tool_name
    orig_args = dict(args)
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
    if tool_name in {"browser_handoff", "computer_handoff"}:
        if not ctx.auto_confirm and not confirmed(ctx.confirmations, connector_slug, tool_name):
            token = sign_confirm(
                "tool",
                {
                    "slug": connector_slug,
                    "tool": tool_name,
                    "args": orig_args,
                    "kind": "handoff",
                },
            )
            pending = {
                "needs_confirm": True,
                "kind": "handoff",
                "token": token,
                "title": "Take control of the computer",
                "preview": {
                    "kind": "handoff",
                    "reason": orig_args.get("reason") or orig_args.get("instruction") or "Finish this step on the live screen.",
                    "instruction": orig_args.get("instruction") or orig_args.get("reason") or "",
                },
            }
            ctx.pending.append(pending)
            return {"status": "awaiting_user", "handoff": True, "reason": pending["preview"]["reason"]}
        return {
            "ok": True,
            "status": "user_finished",
            "handoff": False,
            "message": "The user finished on the live computer. Call browser_snapshot, then continue the remaining steps.",
        }
    if hit is None and spec is None:
        surface = await _load_gateway_surface(ctx, conn)
        execute = surface.get("execute")
        tool_name, args = wrap_through_execute(tool_name, args, execute)
    args = _rewrite_catalog_search_args(tool_name, args, ctx.query)
    destructive = bool(hit.destructive if hit is not None else (spec.destructive if spec else False))
    # Git writes (push, open PR, merge, commit) always need a human, even in auto_confirm runs (schedules, jobs).
    git_write = git_write_gate(conn, connector_slug, tool_name, args)
    if (destructive or git_write) and (git_write or not ctx.auto_confirm) and not confirmed(
        ctx.confirmations, connector_slug, tool_name
    ):
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
        t_invoke = __import__("time").time()
        if ctx.call_impl is not None:
            result = await ctx.call_impl(connector_slug, tool_name, args)
        else:
            result = await invoke_named(ctx.db, ctx.user, conn, tool_name, args, ctx.attachments)
        # #region agent log
        from app.debug_ndjson import dbg

        dbg(
            "C",
            "deep.py:call_user_tool",
            "tool elapsed",
            {
                "slug": connector_slug,
                "orig_tool": orig_tool,
                "tool": tool_name,
                "ms": int((__import__("time").time() - t_invoke) * 1000),
            },
        )

        peek = _dbg_result_peek(result)
        dbg(
            "C",
            "deep.py:call_user_tool",
            "tool invoked",
            {
                "slug": connector_slug,
                "orig_tool": orig_tool,
                "tool": tool_name,
                "args": _dbg_arg_peek(orig_args),
                "wrapped": _dbg_arg_peek(args),
                "has_error": bool(isinstance(result, dict) and result.get("error")),
                "error": (result.get("error")[:160] if isinstance(result, dict) and result.get("error") else ""),
                "has_message_id": bool(isinstance(result, dict) and (result.get("message_id") or (result.get("messages") or [{}])[0].get("message_id"))),
                "attachment_count": len(result.get("attachments") or []) if isinstance(result, dict) else 0,
                **peek,
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
        out = {"error": str(exc)}
        if orig_tool.startswith("browse") and orig_args.get("url"):
            out["url"] = str(orig_args.get("url") or "")
            ctx.results.append(
                {
                    "result_id": f"r{len(ctx.results) + 1}",
                    "source_tool": _source_tool(connector_slug, orig_tool, orig_args),
                    "connector_slug": connector_slug,
                    "tool_name": orig_tool,
                    "args": orig_args,
                    "data": out,
                }
            )
        return out
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
        result = await staff_dispatch(ctx.db, ctx.user, runtime, name, as_dict(args), agent=ctx.agent)
        ctx.sources.append({"source_tool": name, "args": as_dict(args), "snippet": compact_payload(result, 2000)})
        return compact_payload(result)
    except Exception as exc:  # noqa: BLE001
        log.exception("staff tool failed")
        return {"error": str(exc)[:300]}


class SearchToolsInput(BaseModel):
    query: str = Field(description="Short capability phrase for the tool you need, not the full user sentence.")
    connector_slug: str | None = Field(
        default=None,
        description="Leave empty on the first search so every catalog expands. Set only after a data tool from that connector already appeared.",
    )


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


class CreateAgentInput(BaseModel):
    name: str = Field(description="Short display name for the new agent, e.g. 'Dev' or 'Ori'.")
    title: str = Field(default="", description="Role or area, e.g. 'tech work: GitHub and Render'.")
    instructions: str = Field(default="", description="Standing instructions for this agent's focus, tone and limits.")


class UpdateAgentInput(BaseModel):
    agent: str = Field(description="Agent id or name from list_agents_and_groups.")
    name: str | None = Field(default=None, description="New name, to rename it.")
    title: str | None = Field(default=None, description="New role or area.")
    instructions: str | None = Field(default=None, description="Replacement instructions.")
    archived: bool | None = Field(default=None, description="True to archive, false to restore.")


class CreateGroupInput(BaseModel):
    name: str = Field(description="Group name the user gave, e.g. 'Software developers'.")
    members: list[str] = Field(default_factory=list, description="Agent names or ids to add. Leave empty when all_agents is true.")
    all_agents: bool = Field(default=False, description="True when the user said all, every, or all existing agents.")


class GroupMembersInput(BaseModel):
    group: str = Field(description="Group id or name.")
    members: list[str] = Field(description="Agent names or ids.")


class RenameGroupInput(BaseModel):
    group: str = Field(description="Group id or name.")
    name: str = Field(description="New group name.")


class DeleteWorkspaceInput(BaseModel):
    target: str = Field(description="Id or name of the agent or group to delete.")


class AskTeammateInput(BaseModel):
    agent_id: str = Field(description="Id of a teammate in this group.")
    prompt: str = Field(description="Focused task for that teammate. Include only what they need.")


class SetFocusInput(BaseModel):
    focus: str = Field(description="One of: email_calendar, coding, research, errands.")


class SandboxShellInput(BaseModel):
    command: str = Field(description="Shell command to run on the computer (bash). Chain steps with &&.")
    cwd: str | None = Field(default=None, description="Working directory, e.g. ~/work/<repo>.")
    timeout: int = Field(default=300, description="Seconds to wait (10-900). Use more for installs and test suites.")


async def set_agent_focus(ctx: RunContext, focus: str) -> dict:
    from app.agents import service as agents_service
    from app.agents.focus import FOCUSES

    if ctx.agent is None or ctx.db is None:
        return {"error": "No agent to update"}
    try:
        row = await agents_service.update_agent(ctx.db, ctx.user, ctx.agent.id, {"focus": focus})
    except agents_service.AgentError as exc:
        return {"error": str(exc), "options": list(FOCUSES)}
    ctx.agent = row
    ctx.workspace_changed = True
    return {"ok": True, "agent": row.name, "focus": row.focus, "label": FOCUSES[row.focus]["label"] if row.focus else ""}


async def coding_workspace_status(ctx: RunContext) -> dict:
    from app.agent import coding

    probe: dict = {}
    probe_error = ""
    if coding.COMPUTER_SLUG in ctx.connectors:
        result = await call_user_tool(ctx, coding.COMPUTER_SLUG, "exec", {"command": coding.PROBE_COMMAND, "timeout": 40})
        data = as_dict(result)
        inner = as_dict(data.get("data")) if isinstance(data.get("data"), dict) else data
        if data.get("error"):
            probe_error = str(data["error"])
        else:
            probe = coding.parse_probe(str(inner.get("stdout") or inner.get("result") or ""))
    return coding.readiness(ctx.connectors, probe, probe_error)


async def sandbox_shell(ctx: RunContext, command: str, cwd: str | None = None, timeout: int = 300) -> Any:
    from app.agent import coding

    if coding.COMPUTER_SLUG not in ctx.connectors:
        return {"error": coding.readiness(ctx.connectors)["missing"][0]}
    if not (command or "").strip():
        return {"error": "command is required"}
    args: dict[str, Any] = {"command": command, "timeout": coding.clamp_timeout(timeout)}
    if cwd:
        args["cwd"] = cwd
    return await call_user_tool(ctx, coding.COMPUTER_SLUG, "exec", args)


def harness_tools(ctx: RunContext) -> list[StructuredTool]:
    async def list_connectors() -> str:
        """List this user's enabled connectors. Start here when you are unsure which source to use. Other users have different connectors."""
        return dumps_full(await list_user_connectors(ctx))

    async def search_tools_fn(query: str, connector_slug: str | None = None) -> str:
        """Search this user's tool catalog by capability (what the tool must return). First call without connector_slug so mail, MCP, and marketplace catalogs all expand. Pass a short phrase, not the full user sentence. Omit people, brands, and dates. Returns names and descriptions, not data."""
        return dumps_full(await search_user_tools(ctx, query, connector_slug))

    async def get_tool_schema(connector_slug: str, tool_name: str) -> str:
        """Return the description and JSON schema for one tool so you can fill arguments correctly."""
        return dumps_full(await get_user_tool_schema(ctx, connector_slug, tool_name))

    async def call_tool(connector_slug: str, tool_name: str, args: dict[str, Any] | None = None) -> str:
        """Run a tool on one of this user's connectors now. Do not use this to send or fetch if the user named a future time — call schedule_task instead. Catalog/search/resolve/list tools are not answers. Large results are stored; use inspect_result for the slice the user asked for."""
        return dumps(await call_user_tool(ctx, connector_slug, tool_name, args), 60000)

    async def schedule_task(prompt: str) -> str:
        """Queue work for a future time on this agent's own routines. Required when the user says in N minutes, later, tonight, tomorrow, every day, or any clock. Pass their full request. Do not fetch data or send mail in this turn."""
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

    async def ask_teammate(agent_id: str, prompt: str) -> str:
        """Ask another agent in this group to do a slice of the work. They reply in the shared thread. Do not ask yourself."""
        return dumps(await _ask_teammate(ctx, agent_id, prompt), 12000)

    async def list_agents_and_groups() -> str:
        """List this user's own Checkme agents and agent groups (the sidebar under Personal), with ids, titles and members. Use for any question about their Checkme agents or groups. Not a connector."""
        ctx.used_tools.append("list_agents_and_groups")
        return dumps(await workspace_tools.list_agents_and_groups(ctx), 16000)

    async def create_agent(name: str, title: str = "", instructions: str = "") -> str:
        """Create a new Checkme agent for this user (shows in the sidebar under Personal). Use when the user asks to create, add, or set up an agent. Never use a connector for this."""
        ctx.used_tools.append("create_agent")
        return dumps(await workspace_tools.create_agent(ctx, name, title, instructions))

    async def update_agent(
        agent: str,
        name: str | None = None,
        title: str | None = None,
        instructions: str | None = None,
        archived: bool | None = None,
    ) -> str:
        """Rename a Checkme agent or change its title, instructions or archived state."""
        ctx.used_tools.append("update_agent")
        return dumps(await workspace_tools.update_agent(ctx, agent, name, title, instructions, archived))

    async def create_agent_group(name: str, members: list[str] | None = None, all_agents: bool = False) -> str:
        """Create a Checkme agent group (a shared chat room of 2 to 10 of this user's agents). Set all_agents=true for 'all my agents'. Use this, never a connector, for any request to group the user's agents."""
        ctx.used_tools.append("create_agent_group")
        return dumps(await workspace_tools.create_group(ctx, name, members or [], all_agents))

    async def add_group_members(group: str, members: list[str]) -> str:
        """Add agents to an existing Checkme agent group (max 10 members)."""
        ctx.used_tools.append("add_group_members")
        return dumps(await workspace_tools.add_group_members(ctx, group, members))

    async def remove_group_members(group: str, members: list[str]) -> str:
        """Remove agents from a Checkme agent group (a group keeps at least 2 members)."""
        ctx.used_tools.append("remove_group_members")
        return dumps(await workspace_tools.remove_group_members(ctx, group, members))

    async def rename_agent_group(group: str, name: str) -> str:
        """Rename a Checkme agent group."""
        ctx.used_tools.append("rename_agent_group")
        return dumps(await workspace_tools.rename_group(ctx, group, name))

    async def delete_agent(target: str) -> str:
        """Ask to delete a Checkme agent. This only shows the user a confirmation card; it is deleted after they confirm. Chief cannot be deleted."""
        ctx.used_tools.append("delete_agent")
        return dumps(await workspace_tools.request_delete(ctx, "agent", target))

    async def delete_agent_group(target: str) -> str:
        """Ask to delete a Checkme agent group. This only shows the user a confirmation card; it is deleted after they confirm. Member agents are kept."""
        ctx.used_tools.append("delete_agent_group")
        return dumps(await workspace_tools.request_delete(ctx, "group", target))

    tools = [
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
        StructuredTool.from_function(
            coroutine=list_agents_and_groups,
            name="list_agents_and_groups",
            description=list_agents_and_groups.__doc__,
        ),
        StructuredTool.from_function(
            coroutine=create_agent, name="create_agent", description=create_agent.__doc__, args_schema=CreateAgentInput
        ),
        StructuredTool.from_function(
            coroutine=update_agent, name="update_agent", description=update_agent.__doc__, args_schema=UpdateAgentInput
        ),
        StructuredTool.from_function(
            coroutine=create_agent_group,
            name="create_agent_group",
            description=create_agent_group.__doc__,
            args_schema=CreateGroupInput,
        ),
        StructuredTool.from_function(
            coroutine=add_group_members,
            name="add_group_members",
            description=add_group_members.__doc__,
            args_schema=GroupMembersInput,
        ),
        StructuredTool.from_function(
            coroutine=remove_group_members,
            name="remove_group_members",
            description=remove_group_members.__doc__,
            args_schema=GroupMembersInput,
        ),
        StructuredTool.from_function(
            coroutine=rename_agent_group,
            name="rename_agent_group",
            description=rename_agent_group.__doc__,
            args_schema=RenameGroupInput,
        ),
        StructuredTool.from_function(
            coroutine=delete_agent, name="delete_agent", description=delete_agent.__doc__, args_schema=DeleteWorkspaceInput
        ),
        StructuredTool.from_function(
            coroutine=delete_agent_group,
            name="delete_agent_group",
            description=delete_agent_group.__doc__,
            args_schema=DeleteWorkspaceInput,
        ),
    ]
    if ctx.agent is not None and (getattr(ctx.agent, "kind", "custom") or "custom") != "default":

        async def set_my_focus(focus: str) -> str:
            """Set your own focus area when the user says what they want a hand with: email_calendar, coding, research or errands."""
            ctx.used_tools.append("set_my_focus")
            return dumps(await set_agent_focus(ctx, focus))

        tools.append(
            StructuredTool.from_function(
                coroutine=set_my_focus, name="set_my_focus", description=set_my_focus.__doc__, args_schema=SetFocusInput
            )
        )
    from app.agent.coding import is_coding_agent

    if is_coding_agent(ctx.agent):

        async def coding_status() -> str:
            """Check what you can do for coding work right now: computer (sandbox) and toolchain, git push credentials, GitHub and deploy connectors, and exactly what is missing. Call once per coding task, before building."""
            ctx.used_tools.append("coding_workspace_status")
            return dumps(await coding_workspace_status(ctx), 8000)

        async def shell(command: str, cwd: str | None = None, timeout: int = 300) -> str:
            """Run a bash command on the user's computer (Daytona sandbox): git clone/branch/commit/push, rg, sed, package installs, tests, builds. Returns exit_code and output."""
            ctx.used_tools.append("sandbox_shell")
            return dumps(await sandbox_shell(ctx, command, cwd, timeout), 40000)

        tools.extend(
            [
                StructuredTool.from_function(coroutine=coding_status, name="coding_workspace_status", description=coding_status.__doc__),
                StructuredTool.from_function(
                    coroutine=shell, name="sandbox_shell", description=shell.__doc__, args_schema=SandboxShellInput
                ),
            ]
        )
    if ctx.group is not None and ctx.teammate_depth == 0 and ctx.teammates:
        tools.append(
            StructuredTool.from_function(
                coroutine=ask_teammate,
                name="ask_teammate",
                description=ask_teammate.__doc__,
                args_schema=AskTeammateInput,
            )
        )
    return tools


def system_prompt(user, connectors: list, memory: str, agent=None, group=None, teammates: list | None = None) -> str:
    tz = getattr(user, "timezone", None) or "Asia/Kolkata"
    now = _now(tz)
    catalog = catalog_lines(connectors)
    name = getattr(agent, "name", None) or "Chief"
    title = (getattr(agent, "title", None) or "chief of staff").strip() or "chief of staff"
    extra = (getattr(agent, "instructions", None) or "").strip()
    identity = f"You are {name}, this user's {title}. You have mail, calendar, and connected MCP servers.\n"
    if extra:
        identity += extra + "\n"
    from app.agents.focus import focus_prompt

    identity += focus_prompt(agent)
    identity += "Your memory and scheduled routines belong only to you, not to other agents.\n"
    group_bit = ""
    peers = [p for p in (teammates or []) if str(getattr(p, "id", "")) != str(getattr(agent, "id", ""))]
    if group is not None and peers:
        roster = ", ".join(f"{getattr(p, 'name', 'agent')} (id={p.id})" for p in peers)
        group_bit = (
            f"You are working in the group '{getattr(group, 'name', 'group')}' with teammates: {roster}. "
            "Split independent work with ask_teammate(agent_id, prompt). They reply in this thread. "
            "Do not ask yourself. Combine their replies into the user's answer.\n"
        )
    workspace_bit = (
        "Checkme agents and groups: this user's own agents (you, Chief, and the others in the sidebar under Personal) and their groups "
        "are built into this app, not a connector. To list, create, rename or edit agents, or to create, rename, or change members of a group of agents, "
        "use list_agents_and_groups, create_agent, update_agent, create_agent_group, add_group_members, remove_group_members, rename_agent_group. "
        "Never search_tools, list_connectors, or call_tool for these, and never offer a third-party app's 'group' (LiveChat, Slack, Teams, CRM) for them. "
        f"A group has {GROUP_MEMBER_MIN} to {GROUP_MEMBER_MAX} agents; for 'all my agents' pass all_agents=true. "
        "Groups have no separate lead setting. If a tool returns an error about the size, explain it and ask which agents to use. "
        "delete_agent and delete_agent_group only show a confirmation card; say it is waiting for their confirmation. "
        "After creating or changing one, tell the user its name and that it is in the left sidebar under Personal, where they can open it.\n"
    )
    return (
        identity
        + group_bit
        + workspace_bit
        + f"Current date and time: {now.strftime('%Y-%m-%d %H:%M')} ({tz}). Interpret and display times in that timezone.\n"
        "Only use connectors listed below. Other users have different connectors.\n"
        "Discover first: list_connectors, then search_tools with a short capability phrase (what the tool must return), not the full user sentence. "
        "Omit brand names, people, and dates from the search query; pass those as tool arguments after get_tool_schema. "
        "First search_tools without connector_slug so mail, MCP, and marketplace catalogs all expand. "
        "Only pass connector_slug after a data tool from that connector already appeared. "
        "Pick the tool whose name and description match the capability — do not assume mail owns every 'message'. "
        "Mail and analytics are different connectors — do not treat a missing mail tool as a missing MCP connector. "
        "The computer connector is this user's cloud sandbox. The composio connector is an app marketplace of many apps: "
        "search_tools expands its tools; list_connections shows which apps are authorized. "
        "To connect an app the user named, call connect_app with that toolkit slug (gmail, github, slack) — never a different vendor such as a Gmail tool on another app. "
        "connect_app returns redirect_url; the chat shows the Composio connect card. Do not only paste the URL.\n"
        "When the user asks for a chart, plot, HTML report, or downloadable file: fetch the data first, then computer "
        "write_analytics_report with tables and charts, or write_file / run_code plus publish_file. "
        "Cite the download_url (and preview_url). Never cite sandbox: or /tmp paths — those are not downloadable.\n"
        "Data, sales, inventory, KPI, dashboard, and analytics questions: you are a data analyst. Always answer as a report, not a chat recap. "
        "Required sections, in order: title; period/as-of; executive summary; markdown pipe tables for every metric set, time series, and breakdown; findings; recommended actions; downloads. "
        "Numeric rows belong in GitHub-flavored markdown tables (| Metric | Value |), never as bullets or inline 'label: number' lists. "
        "After the data tools return, always call write_analytics_report with the real figures: tables (columns + rows) and charts. "
        "Pick chart types that fit the data: line for a time series, bar for a comparison or ranking, pie for mix/share. "
        "That HTML report includes interactive charts and tables with sort plus filter. Put its download_url in Downloads as a markdown link. "
        "Do this even if the user did not say chart, HTML, or download. Never cite sandbox: or /tmp paths.\n"
        "Pick a source in this order. Do not browse when a connected tool already has the answer.\n"
        "1) Mail, calendar, CRM, sales, inventory, KPI, dashboards, and other already-connected apps: search_tools then call_tool. Do not open the browser.\n"
        "2) Public lookup the user can do in a normal browser — flights, fares, news, hours, prices, people, "
        "Instagram/Twitter/Facebook profiles, Wikipedia, 'just check', 'look up', 'open her/his page': "
        "open the computer browser even if a marketplace app exists. If that app is not in list_connections, do not ask to connect it for a lookup. "
        "browser_navigate Google or the named site so the computer screen shows the page. "
        "Do not search_tools for Instagram/Twitter/Facebook unless list_connections already shows that app.\n"
        "3) A named website or URL, or a page you must click or fill: the computer browser. "
        "Use a connected data tool first only when list_connections shows that app is authorized.\n"
        "This user's computer is one live machine: shell/files and a persistent Chromium tab the right-hand computer screen shows. "
        "Live news or 'what is going on' questions: browser_navigate a Google search URL with the user's words "
        "(https://www.google.com/search?q=...) then open at least two result links. Search the acronym; do not ask what it means first. "
        "If navigate returns chrome-error, chromewebdata, or connection reset, web_fetch the same URL (or another source) and keep going. "
        "web_fetch paints that HTML onto the same computer screen; then browser_snapshot and browser_act on it. Do not wake the computer after a page is showing. "
        "Never stop after one failed page. Never say you cannot verify live updates until you have opened a search and at least one article.\n"
        "End-to-end web work: browser_navigate, browser_snapshot for refs (e0, e1), then browser_act — click, fill, type, select, check, press, scroll. "
        "Fill whole forms from the snapshot: one fill per field, then click submit. "
        "If a page tool returns fallback computer_act, call computer_observe then computer_act (click x,y / type / key / scroll). "
        "Login, 2FA, captcha, payment, or anything you cannot complete: browser_handoff with a short instruction so the user takes control of that same screen. After they finish, snapshot and continue. "
        "Never attach page.png in chat; the computer screen already shows the tab. "
        "Do not replay a completed or uncertain action. That is the computer browser, not Outlook or mail.\n"
        "search_tools already expands gateway catalogs into callable data tools. Prefer those names. "
        "Do not call gateway catalog tools from core_tools unless search_tools returned no data tool.\n"
        "Read get_tool_schema before filling arguments. Argument names come from that schema and description.\n"
        "Search, resolve, list_mcps, and gateway__tool_search return catalogs, not answers. After you pick a data tool, call_tool it. "
        "If a data tool returns a snapshot or as-of date, report those figures and the as-of date — that is still the answer. "
        "If the first search is weak, search once more with different capability words, then call_tool. Do not stop on a catalog search.\n"
        "Call tools as many times as the task needs — different connectors, different argument sets, different windows. Combine results yourself.\n"
        "Gmail and Outlook are mail AND calendar. Never say there is no calendar connector when gmail or outlook is listed. "
        "Send mail with send_message. Create a calendar invite with create_event (attendees, start in IST). "
        "Google Meet or Teams calendar invites: create_google_meet on gmail, create_teams_meeting on outlook. "
        "Chat, DMs, and channel messages on other apps are marketplace or MCP tools, not Outlook search_messages. "
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
        "Destructive connector actions still need call_tool; the app asks the user to confirm.\n"
        "If the user wants work done later (email a report in N minutes, tonight, every day), schedule_task and do not fetch now. "
        "If they want a calendar invite or meeting at a clock time, that time is the event start: call create_event now. "
        "Tell them the scheduled time only when you actually called schedule_task.\n"
        "Never invent emails, events, ids, or numbers that did not come from a tool result. "
        "When connector data comes back, write a detailed analyst report — never a short brief. "
        "Cover what the numbers mean, drivers, comparisons, risks, outliers, and recommended actions, with headings, markdown tables, and a published download link from the tool result.\n\n"
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


async def _ask_teammate(ctx: RunContext, agent_id: str, prompt: str) -> dict:
    if ctx.teammate_depth >= 1:
        return {"error": "A teammate cannot ask another teammate."}
    task = (prompt or "").strip()
    if not task:
        return {"error": "prompt is required"}
    member = next((peer for peer in ctx.teammates if str(getattr(peer, "id", "")) == str(agent_id).strip()), None)
    if member is None:
        return {"error": "That agent is not in this group.", "members": [{"id": str(p.id), "name": p.name} for p in ctx.teammates]}
    if ctx.agent is not None and str(ctx.agent.id) == str(member.id):
        return {"error": "Do not ask yourself. Pick a teammate."}
    if ctx.db is None or ctx.session_id is None:
        return {"error": "No group thread"}
    nested = await run_deep_agent(
        ctx.db,
        ctx.user,
        ctx.session_id,
        ctx.request_id or "peer",
        [],
        task,
        auto_confirm=ctx.auto_confirm,
        workspace_agent=member,
        group=ctx.group,
        teammates=ctx.teammates,
        teammate_depth=ctx.teammate_depth + 1,
        progress=ctx.progress.child(member.name) if ctx.progress is not None else None,
    )
    ctx.used_tools.append(f"ask_teammate:{member.name}")
    for item in nested.get("attachments") or []:
        ctx.attachments.append(item)
    for name in nested.get("used_tools") or []:
        ctx.used_tools.append(str(name))
    if nested.get("workspace_changed"):
        ctx.workspace_changed = True
    for item in nested.get("pending_confirms") or []:
        ctx.pending.append(item)
    for view in nested.get("browser") or []:
        ctx.results.append(
            {
                "result_id": f"r{len(ctx.results) + 1}",
                "tool_name": "browser_navigate",
                "data": view if isinstance(view, dict) else {},
            }
        )
    from app.models import ChatMessage

    ctx.db.add(
        ChatMessage(
            session_id=ctx.session_id,
            user_id=ctx.user.id,
            role="assistant",
            content=nested.get("text") or "",
            meta={
                "agent_id": str(member.id),
                "agent_name": member.name,
                "agent_color": getattr(member, "color", "") or "",
                "peer": True,
                "used_tools": nested.get("used_tools"),
                "computer": nested.get("computer") or {},
                "attachments": nested.get("attachments") or [],
            },
            request_id=ctx.request_id or "",
        )
    )
    try:
        await ctx.db.flush()
    except Exception:  # noqa: BLE001
        log.warning("could not store teammate reply")
    return {"from": member.name, "agent_id": str(member.id), "reply": nested.get("text") or ""}


def _progress_callbacks(ctx: RunContext, conns: list, query: str) -> list:
    if ctx.progress is None:
        return []
    from app.agent.progress import ProgressCallback

    names = {str(c.slug): str(getattr(c, "alias", "") or getattr(c, "name", "") or c.slug) for c in conns}
    agents = {str(getattr(p, "id", "")): str(getattr(p, "name", "")) for p in ctx.teammates}
    return [ProgressCallback(ctx.progress, query=query, connector_names=names, agent_names=agents)]


async def run_deep_agent(
    db: AsyncSession,
    user,
    session_id: uuid.UUID,
    request_id: str,
    history: list[dict],
    query: str,
    confirmations: list[dict] | None = None,
    auto_confirm: bool = False,
    workspace_agent=None,
    group=None,
    teammates: list | None = None,
    teammate_depth: int = 0,
    progress: Any = None,
) -> dict[str, Any]:
    from app.connectors.platform import ensure_platform_connectors
    from app.debug_ndjson import dbg as _dbg_start

    # #region agent log
    _dbg_start("C", "deep.py:run_deep_agent", "agent start", {"query": (query or "")[:80], "n_history": len(history or [])})
    # #endregion
    await ensure_platform_connectors(db, user)
    conns = await user_connectors(db, user.id)
    assignments = await connector_assignment_map(db, user)
    teammate_ids = [getattr(peer, "id", None) for peer in (teammates or [])]
    conns = filter_connectors(
        conns,
        getattr(workspace_agent, "id", None),
        teammate_ids,
        assignments,
    )
    ctx = RunContext(
        db=db,
        user=user,
        connectors={c.slug: c for c in conns},
        confirmations=list(confirmations or []),
        auto_confirm=auto_confirm,
        query=query,
        agent=workspace_agent,
        group=group,
        teammates=list(teammates or []),
        teammate_depth=teammate_depth,
        session_id=session_id,
        request_id=request_id,
        progress=progress,
    )
    callbacks = _progress_callbacks(ctx, conns, query)
    if progress is not None:
        await progress.emit("Recalling what I know about this" if not teammate_depth else "Picking up the task", kind="memory")
    memory = await load_memory(db, user, query, workspace_agent)
    extra = []
    from langchain_core.messages import HumanMessage

    for payload in ctx.confirmations:
        slug, tool = payload.get("slug"), payload.get("tool")
        if not slug or not tool:
            continue
        if progress is not None:
            await progress.emit(f"Running what you confirmed: {tool.replace('_', ' ')}", kind="confirm", tool=tool)
        if slug == workspace_tools.WORKSPACE_SLUG:
            result = await workspace_tools.run_confirmed(ctx, tool, payload.get("args") or {})
            ctx.used_tools.append(tool)
            extra.append(
                HumanMessage(
                    content=f"The user confirmed {tool} in Checkme. Result: {clip(dumps(result, 2000), 2000)}. Tell the user what happened."
                )
            )
            continue
        result = await call_user_tool(ctx, slug, tool, payload.get("args") or {})
        extra.append(
            HumanMessage(
                content=f"The user confirmed {tool} on {slug}. Result: {clip(dumps(result, 4000), 4000)}. Tell the user what happened."
            )
        )
    graph = build_agent(
        ctx,
        system_prompt(user, conns, memory, agent=workspace_agent, group=group, teammates=teammates),
    )
    payload = {"messages": _lc_messages(history, query, extra)}
    limit = max(80, get_settings().max_agent_steps * 8)
    from app.agent.coding import is_coding_agent

    coding = is_coding_agent(workspace_agent)
    if coding:
        # Plan-and-build turns clone, edit, test and push: give them more steps.
        limit = max(limit, 200)
    if teammate_depth:
        limit = min(max(24, get_settings().max_agent_steps * 3), 120 if coding else 48)
    text = ""
    peer = f":peer:{getattr(workspace_agent, 'id', 'x')}" if teammate_depth else ""
    for attempt in range(3):
        try:
            result = await graph.ainvoke(
                payload,
                config={
                    "recursion_limit": limit,
                    "configurable": {"thread_id": f"{session_id}{peer}:r{attempt}"},
                    **({"callbacks": callbacks} if callbacks else {}),
                },
            )
            text = last_text(result.get("messages") if isinstance(result, dict) else [])
            break
        except Exception as exc:  # noqa: BLE001
            if _is_recursion_limit(exc) and attempt < 2:
                limit = min(limit + 40, 280 if coding else 200)
                if progress is not None:
                    await progress.emit("That took many steps; giving it one more pass", kind="retry")
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
    had_card = bool(_connect_cards(ctx))
    await _maybe_start_connect(ctx, query)
    cards = _connect_cards(ctx)
    if cards and not had_card:
        names = ", ".join(str(c.get("name") or c.get("toolkit") or "this app") for c in cards)
        text = f"Connect {names} using the card below. That opens Composio's authorization page."
    await remember(
        db,
        user.id,
        "episodic",
        clip(f"Q: {query}\nA: {text}", 400),
        getattr(workspace_agent, "id", None),
    )
    speaker = None
    if workspace_agent is not None:
        speaker = {
            "id": str(workspace_agent.id),
            "name": workspace_agent.name,
            "color": getattr(workspace_agent, "color", "") or "",
            "title": getattr(workspace_agent, "title", "") or "",
        }
    return {
        "text": text or "Done.",
        "pending_confirms": ctx.pending,
        "attachments": ctx.attachments,
        "used_tools": ctx.used_tools,
        "route": "deep",
        "source": source,
        "mail_threads": _mail_threads(ctx),
        "browser": _browser_views(ctx),
        "computer": _computer_session(ctx),
        "connect_cards": cards,
        "speaker": speaker,
        "group_id": str(group.id) if group is not None else None,
        "workspace_changed": ctx.workspace_changed,
    }
