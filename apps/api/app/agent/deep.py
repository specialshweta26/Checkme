from __future__ import annotations

import asyncio
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
from app.agents.service import (
    GROUP_MEMBER_MAX,
    GROUP_MEMBER_MIN,
    agent_may_use_toolkit,
    connector_assignment_map,
    filter_connectors,
    toolkit_assignment_map,
)
from app.agent.confirm import sign_confirm
from app.agent.invoke import bump_usage, confirmed, invoke_named, preview
from app.agent.memory import load_memory
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


JOB_CONFIRM_TTL = 3 * 24 * 3600


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
    thread: list = field(default_factory=list)
    session_id: uuid.UUID | None = None
    request_id: str = ""
    workspace_changed: bool = False
    progress: Any = None
    job: Any = None  # AgentJob when running in the background worker
    jobs_started: list = field(default_factory=list)  # jobs this turn queued (start_coding_job / delegate)
    question: dict | None = None  # ask_user: {question, options, waiting_for?} -> job waits / chat buttons
    prs: list = field(default_factory=list)  # PRs opened or updated this turn
    merged: list = field(default_factory=list)  # PRs merged this turn
    repo: str = ""  # repo this turn worked on
    overviews: dict = field(default_factory=dict)  # repo_overview cache per repo
    check_results: dict = field(default_factory=dict)  # last run_checks per repo (for the PR body)
    check_attempts: dict = field(default_factory=dict)
    toolkit_assignments: dict = field(default_factory=dict)
    scratchpad: list = field(default_factory=list)  # this-chat checklist (scratchpad_*)
    active_skills: list = field(default_factory=list)  # skills routed to / loaded by this task
    task_key: str = ""  # isolates this task's skill state (app/skills/task_state.py)
    task_state: dict | None = None  # the skill this task follows and how far it got (checkpointed)
    task_resumed: bool = False


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
            allowed = [
                row
                for row in (conns if isinstance(conns, list) else [])
                if _active_toolkit(row) and agent_may_use_toolkit(getattr(ctx.agent, "id", None), _active_toolkit(row), ctx.toolkit_assignments)
            ]
            toolkit = matching_toolkit(allowed, extra_capability_terms(query))
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
_ACTIVE_CONNECTION = frozenset({"ACTIVE", "SUCCESS", "ENABLED", "CONNECTED"})


def _is_connect_query(query: str) -> bool:
    q = (query or "").lower().replace("connected", " ")
    return any(k in q for k in _CONNECT_HINTS)


def _active_toolkit(row: dict) -> str:
    status = str(row.get("status") or "").upper()
    if status and status not in _ACTIVE_CONNECTION:
        return ""
    return str(row.get("toolkit") or row.get("slug") or "").strip().lower()


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
    linked = await call_user_tool(ctx, conn.slug, "list_connections", {})
    rows = linked.get("connections") if isinstance(linked, dict) else []
    already = any(_active_toolkit(row) == toolkit for row in (rows if isinstance(rows, list) else []))
    await _grant_connect_agent(ctx, toolkit)
    if already:
        return
    await call_user_tool(ctx, conn.slug, "connect_app", {"toolkit": toolkit})
    await _grant_connect_agent(ctx, toolkit)


async def _grant_connect_agent(ctx: RunContext, toolkit: str) -> None:
    agent = ctx.agent
    if ctx.db is None or ctx.user is None or agent is None or not getattr(agent, "id", None):
        return
    try:
        from app.agents import service as agents_service

        await agents_service.grant_toolkit_agent(ctx.db, ctx.user, toolkit, agent.id)
    except Exception:  # noqa: BLE001
        return


def _connect_cards(ctx: RunContext) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    agent = ctx.agent
    agent_id = str(getattr(agent, "id", "") or "")
    agent_name = str(getattr(agent, "name", "") or "")
    for row in ctx.results:
        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        url = str(data.get("redirect_url") or "").strip()
        key = url or f"key:{data.get('toolkit') or ''}"
        if (not url and not data.get("needs_credentials")) or key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "toolkit": data.get("toolkit") or "",
                "name": data.get("name") or data.get("toolkit") or "Connect app",
                "logo": data.get("logo") or "",
                "description": data.get("description") or "",
                "redirect_url": url,
                "needs_credentials": bool(data.get("needs_credentials")),
                "fields": data.get("fields") or [],
                "agent_id": agent_id,
                "agent_name": agent_name,
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
    out.extend(await _connected_marketplace_apps(ctx))
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


async def _connected_marketplace_apps(ctx: RunContext) -> list[dict]:
    conn = next((c for c in ctx.connectors.values() if getattr(c, "provider", None) == "composio"), None)
    if conn is None:
        return []
    try:
        linked = await _silent_call(ctx, conn, "list_connections", {})
    except Exception:  # noqa: BLE001
        return []
    rows = linked.get("connections") if isinstance(linked, dict) else []
    if not isinstance(rows, list):
        return []
    names: dict[str, str] = {}
    if ctx.db is not None:
        try:
            from app.connectors.composio_box import list_toolkits

            for app in await list_toolkits("", 2000):
                slug = str(app.get("slug") or "").strip().lower()
                if slug:
                    names[slug] = str(app.get("name") or slug)
        except Exception:  # noqa: BLE001
            names = {}
    out = []
    seen: set[str] = set()
    agent_id = getattr(ctx.agent, "id", None)
    for row in rows:
        slug = _active_toolkit(row)
        if not slug or slug in seen:
            continue
        if not agent_may_use_toolkit(agent_id, slug, ctx.toolkit_assignments):
            continue
        seen.add(slug)
        out.append(
            {
                "slug": slug,
                "name": names.get(slug) or slug,
                "kind": "marketplace",
                "connected": True,
                "description": f"Connected {names.get(slug) or slug} account. search_tools then call_tool on composio.",
                "via": "composio",
            }
        )
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
            "xlsx",
            "spreadsheet",
            "pandas",
            "deep dive",
            "download",
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
            "computer",
            "click",
            "sign in",
            "log in",
            "availability",
            "price",
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
                    "browser_tabs",
                    "browser_new_tab",
                    "browser_switch_tab",
                    "browser_close_tab",
                    "computer_observe",
                    "computer_act",
                    "browser_handoff",
                    "web_fetch",
                    "browser_search",
                    "browse_open",
                    "browse_act",
                    "publish_file",
                    "run_code",
                    "write_analytics_report",
                    "read_file",
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
    from app.connectors.github import is_git_write

    from app.agent.deploy import is_deploy_write

    text = f"{getattr(conn, 'name', '') or ''} {getattr(conn, 'alias', '') or ''} {getattr(conn, 'server_url', '') or ''}"
    if is_git_write(str(getattr(conn, "provider", "") or ""), slug, tool, args, text):
        return True
    # Deploy writes through a Render / Vercel / … connector are just as destructive: always a card.
    return is_deploy_write(conn, slug, tool, args)


_APPROVAL_KEYS = ("repo", "number", "head", "head_sha", "branch", "method", "base", "state", "delete_branch")


def _approved(ctx: RunContext, conn, slug: str, tool: str, args: dict) -> bool:
    """Was this exact action approved? GitHub writes (merge, push, PR edits) must match repo, PR, head sha and
    options, so approving one merge never lets a different PR (or a moved branch) through."""
    if not confirmed(ctx.confirmations, slug, tool):
        return False
    from app.connectors.github import PROVIDERS, WRITE_TOOLS

    if str(getattr(conn, "provider", "") or "") not in PROVIDERS or tool not in WRITE_TOOLS:
        return True

    def key(a: dict) -> tuple:
        a = dict(a or {})
        if tool == "merge_pull_request":
            from app.connectors.github import merge_method

            a["method"] = merge_method(a.get("method"))
        return tuple("" if a.get(k) in (None, False, "") else str(a.get(k)).lower() for k in _APPROVAL_KEYS)

    want = key(args)
    return any(c.get("slug") == slug and c.get("tool") == tool and key(c.get("args") or {}) == want for c in ctx.confirmations)


async def _track_github_writes(ctx: RunContext, conn, tool: str, args: dict, result: Any) -> None:
    """PRs opened and merged through Composio GitHub go on the job (Merge it? question, merge sha) and in coding memory."""
    from app.connectors.github import PROVIDERS

    if str(getattr(conn, "provider", "") or "") not in PROVIDERS or not isinstance(result, dict) or result.get("error"):
        return
    if tool not in ("create_pull_request", "merge_pull_request"):
        return
    from app.agent import coding_memory

    repo = str(args.get("repo") or "")
    rec = getattr(ctx.progress, "record", None) if ctx.progress is not None else None
    if tool == "create_pull_request" and result.get("url"):
        pr = {"url": result["url"], "repo": repo, "number": result.get("number"), "branch": args.get("head"), "title": args.get("title")}
        if not any(p.get("url") == pr["url"] for p in ctx.prs):
            ctx.prs.append(pr)
        ctx.repo = ctx.repo or repo
        if rec is not None:
            await rec("pr", f"Pull request: {pr['url']}", pr)
        await coding_memory.note(ctx, repo=repo, pr=pr)
    elif tool == "merge_pull_request" and result.get("merged"):
        m = {"repo": repo, "number": int(args.get("number") or 0), "sha": result.get("sha"), "method": result.get("method"), "url": result.get("url")}
        ctx.merged.append(m)
        ctx.repo = ctx.repo or repo
        if rec is not None:
            await rec("pr", f"Merged {repo}#{m['number']} ({m.get('method') or 'merge'}): {str(m.get('sha') or '')[:12]}", m)
        url = m.get("url") or f"https://github.com/{repo}/pull/{m['number']}"
        await coding_memory.note(ctx, repo=repo, pr={"url": url, "repo": repo, "number": m["number"], "branch": "", "state": "merged"})


async def call_user_tool(ctx: RunContext, connector_slug: str, tool_name: str, args: dict | None = None) -> Any:
    from app.agent import cancel as run_cancel

    run_cancel.check()  # the user pressed Stop: end at this step boundary
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
    if (destructive or git_write) and (git_write or not ctx.auto_confirm) and not _approved(
        ctx, conn, connector_slug, tool_name, args
    ):
        # Background jobs wait for a conversational reply, so their signed confirmations live longer than chat ones.
        ttl = JOB_CONFIRM_TTL if (ctx.job is not None or ctx.auto_confirm) else 600
        token = sign_confirm("tool", {"slug": connector_slug, "tool": tool_name, "args": args}, ttl=ttl)
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
        if connector_slug == "computer":
            from app.connectors.daytona_box import set_computer_agent

            set_computer_agent(str(getattr(ctx.agent, "id", "") or ""), getattr(ctx.agent, "name", "") or "")
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
        await _track_github_writes(ctx, conn, tool_name, args, result)
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
        result = await staff_dispatch(ctx.db, ctx.user, runtime, name, as_dict(args), agent=ctx.agent, session_id=ctx.session_id)
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
    args: dict[str, Any] = Field(
        default_factory=dict,
        description="For create_automation: name, trigger, condition, action, requires_approval, and optional trigger_type='webhook' plus agent_name. Webhook trigger must be an exact event type such as issues or push.",
    )


class ScheduleTaskInput(BaseModel):
    prompt: str = Field(
        description="The user's full request, including what to do and when (in N minutes, tonight, every day, a clock time)."
    )
    agent_name: str = Field(
        default="",
        description="Optional: the name of another of the user's agents that should own and run it (e.g. Coder). Empty means you.",
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
    prompt: str = Field(
        description="In-thread message for that teammate. Address them by name and include what they need. Their reply posts as their own bubble."
    )


class SaveSkillInput(BaseModel):
    name: str = Field(description="Short name for this workflow.")
    instruction: str = Field(default="", description="What to do when this skill runs.")


class GetSkillInput(BaseModel):
    skill_id: str = Field(default="", description="Skill id from your skill index or list_my_skills.")
    name: str = Field(default="", description="Skill name, when you do not have the id.")


class SkillProgressInput(BaseModel):
    step: int = Field(description="How many steps of the skill are done now (1 after the first step).")
    note: str = Field(default="", description="One line on what this step did or found.")
    artifact_label: str = Field(default="", description="Name of something produced (link, file, id, quote). Optional.")
    artifact_value: str = Field(default="", description="Its value. Optional.")
    done: bool = Field(default=False, description="True when the whole skill is finished.")


class StopTeachingInput(BaseModel):
    name: str = Field(default="", description="Name for the taught skill.")


class SetFocusInput(BaseModel):
    focus: str = Field(description="One of: email_calendar, coding, research, errands.")


class SandboxShellInput(BaseModel):
    command: str = Field(description="Shell command to run on the computer (bash). Chain steps with &&.")
    cwd: str | None = Field(default=None, description="Working directory, e.g. ~/work/<repo>.")
    timeout: int = Field(default=300, description="Seconds to wait (10-900). Use more for installs and test suites.")


class GitCheckoutInput(BaseModel):
    repo: str = Field(description="owner/name, e.g. octocat/hello-world")
    branch: str = Field(default="", description="Work branch to create or reuse. Use agent/<short-name> for changes you will push.")
    base: str = Field(default="", description="Branch to start from. Defaults to the repo default branch.")


class RunTestsInput(BaseModel):
    repo: str = Field(description="owner/name (already checked out)")
    command: str = Field(default="", description="Test command. Empty = auto-detect (npm test, pytest, go test, cargo test).")
    timeout: int = Field(default=600, description="Seconds (10-900).")


class GitDiffInput(BaseModel):
    repo: str = Field(description="owner/name (already checked out)")
    base: str = Field(default="", description="Compare against origin/<base>. Defaults to the remote default branch.")


class GitCommitInput(BaseModel):
    repo: str = Field(description="owner/name (already checked out, on an agent/* branch)")
    message: str = Field(description="Commit message: a short summary line, then details if useful.")
    paths: list[str] | None = Field(default=None, description="Only these paths. Empty = all changes.")


class StartJobInput(BaseModel):
    task: str = Field(description="Self-contained task: the repo (owner/name), what to change or check, and acceptance criteria.")
    title: str = Field(default="", description="Short title for the timeline.")


class BackgroundTaskInput(BaseModel):
    task: str = Field(description="Self-contained task: the goal, constraints (dates, places, sources, limits) and what to deliver.")
    title: str = Field(default="", description="Short title for the timeline.")


async def start_coding_job(ctx: "RunContext", task: str, title: str = "", role: str = "planner") -> dict:
    """Queue a background job for this agent in the current chat (chat turns only)."""
    from app.jobs import service as jobs
    from app.jobs.worker import wake

    if ctx.db is None or ctx.agent is None:
        return {"error": "Background jobs need a saved agent."}
    try:
        job = await jobs.create_job(
            ctx.db, ctx.user, agent=ctx.agent, prompt=task, title=title, role=role, kind="chat", session_id=ctx.session_id
        )
    except jobs.JobError as exc:
        return {"error": str(exc)}
    await ctx.db.commit()
    wake()
    info = jobs.serialize_job(job)
    ctx.jobs_started.append(info)
    return {
        "started": True,
        "job_id": info["id"],
        "title": info["title"],
        "note": "The job is running in the background; the user sees its live timeline under this reply. "
        "Reply in one or two lines that it started; do not do the work yourself in this turn.",
    }


class DelegateInput(BaseModel):
    role: str = Field(description="coder, reviewer or planner")
    task: str = Field(description="Self-contained instructions for the child job.")
    agent_name: str = Field(default="", description="Optional: which of the user's agents runs it. Defaults to you.")
    title: str = Field(default="", description="Short title for the timeline.")


async def delegate_job(ctx: "RunContext", role: str, task: str, agent_name: str = "", title: str = "") -> dict:
    """Create a child job under the current background job (depth capped, fan-out capped per turn)."""
    from app.agents.service import find_agent_by_name
    from app.jobs import service as jobs

    parent = ctx.job
    if parent is None or ctx.db is None:
        return {"error": "delegate only works inside a background job."}
    role = (role or "").strip().lower()
    if role not in jobs.ROLES:
        return {"error": f"role must be one of {', '.join(jobs.ROLES)}"}
    if len(ctx.jobs_started) >= jobs.MAX_CHILDREN:
        return {"error": f"At most {jobs.MAX_CHILDREN} delegated jobs per step. Wait for these to finish."}
    agent = ctx.agent
    if agent_name.strip():
        agent = await find_agent_by_name(ctx.db, ctx.user, agent_name)
        if agent is None:
            return {"error": f"No agent named {agent_name}."}
    try:
        child = await jobs.create_job(
            ctx.db, ctx.user, agent=agent, prompt=task, title=title, role=role, kind="delegate", parent=parent
        )
    except jobs.JobError as exc:
        return {"error": str(exc)}
    await ctx.db.flush()
    info = jobs.serialize_job(child)
    ctx.jobs_started.append(info)
    handed = await _hand_over_state(ctx, getattr(agent, "id", None), getattr(agent, "name", "") or role, f"job:{child.id}", job_id=child.id)
    if handed:
        child.meta = {**(child.meta or {}), "skill_state_id": handed["id"], "skill_name": handed.get("skill_name")}
        info["skill_state"] = handed
    if ctx.progress is not None and hasattr(ctx.progress, "record"):
        await ctx.progress.record("status", f"Delegated to {info['agent_name'] or role} ({role}): {info['title']}", {"child_id": info["id"]})
    return {
        "delegated": True,
        "job_id": info["id"],
        "role": role,
        "note": "The child job runs after this turn. End your turn now with one line saying what you handed off; "
        "you will be resumed with its result.",
    }


class PushBranchInput(BaseModel):
    repo: str = Field(description="owner/name")
    branch: str = Field(description="The agent/* branch to push. Other branches are refused.")
    open_pr: bool = Field(default=True, description="Also open a pull request after pushing.")
    title: str = Field(default="", description="Pull request title.")
    body: str = Field(default="", description="Pull request description: what changed, why, and how it was tested.")
    base: str = Field(default="", description="PR target branch. Defaults to the repo default branch.")
    draft: bool = Field(default=False, description="Open the PR as a draft.")
    pr_number: int = Field(
        default=0,
        description="Only when the user asked you to update an existing PR: its number. Then branch must be that PR's head "
        "branch (verified), and no new PR is opened.",
    )


class GitInspectInput(BaseModel):
    repo: str = Field(description="owner/name (already checked out with git_checkout)")
    action: str = Field(description="status | branches | log | show | diff | blame | grep | tree | read")
    ref: str = Field(default="", description="Commit, branch or tag (log/show/diff/blame/tree/read).")
    ref2: str = Field(default="", description="Second ref for diff (ref..ref2).")
    path: str = Field(default="", description="File or directory path.")
    query: str = Field(default="", description="Text to search for (grep).")
    start: int = Field(default=0, description="First line (read/blame).")
    end: int = Field(default=0, description="Last line (read/blame).")
    limit: int = Field(default=50, description="Max commits/results.")
    regex: bool = Field(default=False, description="grep: treat query as a regex instead of plain text.")


class ToolchainInput(BaseModel):
    extras: list[str] = Field(default_factory=list, description="Extra toolchains only when the repo needs them: go, java, rust.")
    force: bool = Field(default=False, description="Re-check everything even if the toolchain marker exists.")


class RepoOnlyInput(BaseModel):
    repo: str = Field(description="owner/name (already checked out)")


class SymbolInput(BaseModel):
    repo: str = Field(description="owner/name (already checked out)")
    symbol: str = Field(description="Function, class, type or variable name, e.g. parse_config or UserService.")
    limit: int = Field(default=40, description="Max results.")


class ReferencesInput(BaseModel):
    repo: str = Field(description="owner/name (already checked out)")
    symbol: str = Field(description="Identifier to find usages of (whole word).")
    path: str = Field(default="", description="Only under this directory or file.")
    limit: int = Field(default=80, description="Max lines.")


class ReadRangeInput(BaseModel):
    repo: str = Field(description="owner/name (already checked out)")
    path: str = Field(description="File path in the repo.")
    start: int = Field(default=1, description="First line.")
    end: int = Field(default=200, description="Last line.")
    ref: str = Field(default="", description="Read the file at this commit/branch instead of the working tree.")


class CheckCommand(BaseModel):
    kind: str = Field(default="unit", description="lint | typecheck | unit | integration | build | setup")
    command: str
    cwd: str = Field(default=".", description="Directory inside the repo.")


class RunChecksInput(BaseModel):
    repo: str = Field(description="owner/name (already checked out)")
    kinds: list[str] = Field(default_factory=list, description="Subset of lint, typecheck, unit, integration, build. Empty = all found.")
    commands: list[CheckCommand] = Field(default_factory=list, description="Override: exact commands to run instead of the detected ones.")
    timeout_each: int = Field(default=600, description="Seconds per command (10-900).")


class Finding(BaseModel):
    path: str = Field(default="", description="File path (as in the PR diff).")
    line: int = Field(default=0, description="Line number in the new version of the file; inline only when it is inside the diff.")
    severity: str = Field(default="minor", description="blocker | major | minor | nit")
    body: str = Field(description="What is wrong and how to fix it.")


class ReviewInput(BaseModel):
    repo: str = Field(description="owner/name")
    number: int = Field(description="Pull request number")
    verdict: str = Field(default="COMMENT", description="COMMENT, REQUEST_CHANGES, or APPROVE (APPROVE only when nothing blocks).")
    summary: str = Field(default="", description="2-5 sentences: what the PR does and the overall assessment.")
    findings: list[Finding] = Field(default_factory=list, description="Concrete issues, each with path/line when possible.")
    include_checks: bool = Field(default=True, description="Add the latest run_checks table to the review.")


class DeployParams(BaseModel):
    service: str = Field(default="", description="Render srv-… id, Cloud Run / App Engine service, Railway service")
    app: str = Field(default="", description="Fly app, Vercel project, Azure Container App")
    stack: str = Field(default="", description="AWS CloudFormation stack")
    stage: str = Field(default="", description="Serverless stage")
    region: str = Field(default="", description="Cloud region when the platform needs one")
    group: str = Field(default="", description="Azure resource group")
    ref: str = Field(default="", description="Commit sha to deploy (Render)")
    deployment: str = Field(default="", description="Deploy id / URL (status, logs, rollback target)")
    version: str = Field(default="", description="Version / revision for rollback (App Engine, Cloud Run, Azure)")
    image: str = Field(default="", description="Image to roll back to (Fly)")
    flavor: str = Field(default="", description="aws: serverless|sam|cdk; gcloud: appengine|run")
    production: bool = Field(default=True, description="Vercel / Netlify: production (true) or preview (false)")


class DetectDeployInput(BaseModel):
    repo: str = Field(description="owner/name (cloned onto the computer automatically if it is not there yet)")


class GenerateDeployConfigInput(BaseModel):
    repo: str = Field(description="owner/name (cloned automatically if needed)")
    platform: str = Field(default="", description="render, vercel, fly, netlify, railway, gcloud or azure; empty picks a default for the stack")
    cwd: str = Field(default=".", description="Directory inside the repo")
    write: bool = Field(default=True, description="Write the files into the working tree (never overwrites existing ones)")


class DeployInput(BaseModel):
    repo: str = Field(description="owner/name (cloned onto the computer automatically if it is not there yet)")
    platform: str = Field(default="", description="render, vercel, fly, netlify, railway, aws, gcloud or azure: the one the user named; empty picks a sensible default (say which)")
    params: DeployParams = Field(default_factory=DeployParams)
    cwd: str = Field(default=".", description="Directory inside the repo (from detect_deploy_targets)")
    use_cli: bool = Field(default=False, description="Use the CLI even though a connector for this platform exists")


class DeployReadInput(BaseModel):
    repo: str = Field(default="", description="owner/name (optional for status)")
    platform: str = Field(description="render, vercel, fly, netlify, railway, aws, gcloud or azure")
    params: DeployParams = Field(default_factory=DeployParams)
    cwd: str = Field(default=".", description="Directory inside the repo")


class AskUserInput(BaseModel):
    question: str = Field(description="One short question.")
    options: list[str] = Field(description="2-5 short answer options shown as buttons, e.g. repo names or Yes / No.")
    waiting_for: str = Field(default="", description="Rarely needed: sign-in waits (login_<platform>) are set automatically by deploy / git_checkout / push_branch. Set login_<platform> only if you hand over a sign-in link yourself, so the job resumes when it completes.")


class RememberCodingInput(BaseModel):
    default_repo: str = Field(default="", description="owner/name the user works in by default.")
    branch_convention: str = Field(default="", description="e.g. agent/<ticket>-<slug>, or feat/<name>.")
    base_branch: str = Field(default="", description="Branch PRs usually target, e.g. main or develop.")
    notes: str = Field(default="", description="Other lasting preferences (test command, reviewers, merge method).")


class LearnAgentMemoryInput(BaseModel):
    category: str = Field(default="preference", description="Short label such as preference, routine, family, work, or project.")
    fact: str = Field(description="One durable, non-sensitive preference or working detail to remember for this agent.")


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
        result = await call_user_tool(ctx, coding.COMPUTER_SLUG, "exec", {"command": coding.bash(coding.PROBE_COMMAND), "timeout": 60})
        data = as_dict(result)
        inner = as_dict(data.get("data")) if isinstance(data.get("data"), dict) else data
        if data.get("error"):
            probe_error = str(data["error"])
        else:
            probe = coding.parse_probe(str(inner.get("stdout") or inner.get("result") or ""))
    from app.agent import deploy
    from app.agent.git_tools import github_status

    out = coding.readiness(ctx.connectors, probe, probe_error, await github_status(ctx))
    return deploy.add_readiness(out, ctx.connectors, await deploy.saved_env_names(ctx))


async def sandbox_shell(ctx: RunContext, command: str, cwd: str | None = None, timeout: int = 300) -> Any:
    from app.agent import coding

    if coding.COMPUTER_SLUG not in ctx.connectors:
        return {"error": coding.readiness(ctx.connectors)["missing"][0]}
    if not (command or "").strip():
        return {"error": "command is required"}
    from app.agent import computer_harness

    # Same path as every agent's shell: toolchain PATH, and a missing known tool is installed and retried once.
    return await computer_harness.cloud_exec(ctx, command, cwd, coding.clamp_timeout(timeout))


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

    async def schedule_task(prompt: str, agent_name: str = "") -> str:
        """Queue work for a future time on this agent's own routines (or on a named agent with agent_name). Required when the user says in N minutes, later, tonight, tomorrow, every day, or any clock. Pass their full request. Do not fetch data or send mail in this turn. Scheduled git pushes, PRs and merges still wait for the user's approval."""
        return dumps(await run_staff(ctx, "schedule_task", {"prompt": prompt, "agent_name": agent_name}))

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
        """Run a staff capability (briefing, inbox, commitments, schedules, webhook automations, or voice). For a webhook pass trigger_type='webhook', exact event name, action, optional condition and requires_approval (true by default)."""
        return dumps(await run_staff(ctx, name, args))

    async def remember_for_me(category: str = "preference", fact: str = "") -> str:
        """Remember a lasting, non-sensitive fact for yourself. It is private to this agent and never shared with teammates."""
        from app.agent.memory import learn_for_agent

        ctx.used_tools.append("remember_for_me")
        return dumps(await learn_for_agent(ctx.db, ctx.user, ctx.agent, category, fact), 1500)

    async def ask_teammate(agent_id: str, prompt: str) -> str:
        """Post a message to a teammate in this group thread. They reply as themselves in their own bubble. Their memory stays separate. Do not ask yourself or paste their reply into yours."""
        return dumps(await _ask_teammate(ctx, agent_id, prompt), 12000)

    async def save_skill(name: str, instruction: str = "") -> str:
        """Save a workflow this bot should reuse later."""
        from app.skills.service import save_skill as _save

        if ctx.db is None:
            return dumps({"error": "Cannot save a skill without a workspace."})
        row = await _save(
            ctx.db,
            ctx.user,
            name=name,
            agent_id=getattr(ctx.agent, "id", None),
            instruction=instruction,
            kind="saved",
        )
        return dumps({"ok": True, "id": str(row.id), "name": row.name})

    async def list_my_skills() -> str:
        """List all of your skills (saved and taught workflows) with id, status and a one-line description of when to use each."""
        from app.skills.profile import one_liner, usable
        from app.skills.service import list_skills as _list

        ctx.used_tools.append("list_my_skills")
        if ctx.db is None:
            return dumps({"skills": []})
        rows = usable(await _list(ctx.db, ctx.user, getattr(ctx.agent, "id", None)))
        return dumps(
            {"skills": [{"id": r["id"], "name": r["name"], "kind": r["kind"], "status": r.get("status"), "use_when": one_liner(r), "steps": r.get("step_count", 0)} for r in rows]},
            12000,
        )

    async def get_skill(skill_id: str = "", name: str = "") -> str:
        """Load one of your skills in full (instructions, recorded steps, narration transcript) by id or name, before following it."""
        from app.skills.service import get_skill as _get, serialize_skill

        ctx.used_tools.append("get_skill")
        if ctx.db is None:
            return dumps({"error": "Cannot read skills without a workspace."})
        row = await _get(ctx.db, ctx.user, skill_id, name, getattr(ctx.agent, "id", None))
        if row is None:
            return dumps({"error": "Unknown skill. Call list_my_skills for ids."})
        data = serialize_skill(row, full=True)
        data.pop("frames", None)
        if not any(str(s.get("id")) == data["id"] for s in ctx.active_skills):
            ctx.active_skills.append({"id": data["id"], "name": data["name"], "match": {"by": "loaded"}})
        if ctx.task_key and (ctx.task_state is None or str(ctx.task_state.get("skill_id")) != data["id"]):
            await _begin_task_state(ctx, data, resumed_ok=False)
        if ctx.progress is not None:
            await ctx.progress.emit(f"Using skill: {data['name']}", kind="skill", tool="get_skill")
        return dumps(data, 16000)

    async def skill_progress(step: int, note: str = "", artifact_label: str = "", artifact_value: str = "", done: bool = False) -> str:
        """Checkpoint the skill this task is following: steps done, a progress note, anything produced. Saved at once, so the task resumes here after a restart or handover."""
        from app.skills import task_state as ts

        ctx.used_tools.append("skill_progress")
        if not ctx.task_state:
            return dumps({"error": "This task is not following a skill. Load one with get_skill first."})
        by = getattr(ctx.agent, "name", "") or ""
        artifact = {"label": artifact_label, "value": artifact_value} if (artifact_label or artifact_value) else None
        state = await ts.checkpoint(ctx.task_state["id"], step_index=step, note=note, artifact=artifact, by=by)
        if state is None:
            return dumps({"error": "Task state is gone."})
        if done:
            state = await ts.finish(state["id"], note or "Done.", by=by) or state
        ctx.task_state = state
        await _narrate_task_state(ctx, note)
        return dumps({"saved": True, "state": ts.serialize(state)}, 4000)

    async def start_teaching() -> str:
        """Record the computer while the user demonstrates a workflow (the user may narrate out loud from the screen panel). Max ten minutes by default."""
        from app.skills.service import start_teaching as _start

        return dumps(await _start(ctx.user, getattr(ctx.agent, "id", None)))

    async def stop_teaching(name: str = "") -> str:
        """Stop recording and save the taught skill (actions plus any narration; the instruction is written up in the background)."""
        from app.skills.service import stop_teaching as _stop

        if ctx.db is None:
            return dumps({"error": "Cannot save a taught skill without a workspace."})
        return dumps(await _stop(ctx.db, ctx.user, name))

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
        """Ask to delete a Checkme agent. Explain the deletion in chat and wait for the user's clear reply before deleting. Chief cannot be deleted."""
        ctx.used_tools.append("delete_agent")
        return dumps(await workspace_tools.request_delete(ctx, "agent", target))

    async def delete_agent_group(target: str) -> str:
        """Ask to delete a Checkme agent group. Explain the deletion in chat and wait for the user's clear reply before deleting. Member agents are kept."""
        ctx.used_tools.append("delete_agent_group")
        return dumps(await workspace_tools.request_delete(ctx, "group", target))

    from app.agent.harness_extra import extra_harness_tools

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
    ]
    tools.extend(extra_harness_tools(ctx))
    tools.extend(
        [
        StructuredTool.from_function(
            coroutine=remember_for_me,
            name="remember_for_me",
            description=remember_for_me.__doc__,
            args_schema=LearnAgentMemoryInput,
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
    )
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
            """Check what you can do for coding work right now: computer (sandbox) and dev toolchain, git credentials, the Composio GitHub connection and deploy routes. GitHub and deploy platforms are never blockers: the tools sign in through a link when needed. Call once per coding task, before building."""
            ctx.used_tools.append("coding_workspace_status")
            return dumps(await coding_workspace_status(ctx), 8000)

        async def shell(command: str, cwd: str | None = None, timeout: int = 300) -> str:
            """Run a bash command on the user's cloud computer (E2B sandbox, or Daytona when configured): read and edit files (rg, sed, cat, heredocs, python patches), install packages, run builds. For git use git_checkout, git_diff, git_commit and push_branch instead: they handle GitHub auth safely. Never put tokens or credentials in a command. Returns exit_code and output."""
            ctx.used_tools.append("sandbox_shell")
            return dumps(await sandbox_shell(ctx, command, cwd, timeout), 40000)

        from app.agent import coding as coding_mod
        from app.agent import git_tools

        async def checkout(repo: str, branch: str = "", base: str = "") -> str:
            """Clone (or update) a GitHub repo into ~/work/<name> on the computer and check out a branch. Tries the Composio GitHub token, anonymous / the computer's gh credentials, then a snapshot; if none works it starts the GitHub CLI sign-in and returns status=awaiting_user_login with the link (give it to the user in one line; the job resumes). Start every code change here with branch=agent/<short-name>."""
            ctx.used_tools.append("git_checkout")
            return dumps(await git_tools.git_checkout(ctx, repo, branch, base), 20000)

        async def tests(repo: str, command: str = "", timeout: int = 600) -> str:
            """Run the repo's tests on the computer (auto-detects npm test / pytest / go test / cargo test, or pass command). Returns exit_code, passed and the output tail."""
            ctx.used_tools.append("run_tests")
            return dumps(await git_tools.run_tests(ctx, repo, command, timeout), 20000)

        async def diff(repo: str, base: str = "") -> str:
            """Show status, commits and the diff of the checked-out repo against the base branch. Use before committing and before push_branch."""
            ctx.used_tools.append("git_diff")
            return dumps(await git_tools.git_diff(ctx, repo, base), 20000)

        async def commit(repo: str, message: str, paths: list[str] | None = None) -> str:
            """Commit changes in the checked-out repo on your work branch (refuses the default branch, main/master and detached HEAD)."""
            ctx.used_tools.append("git_commit")
            return dumps(await git_tools.git_commit(ctx, repo, message, paths), 8000)

        async def push(
            repo: str,
            branch: str,
            open_pr: bool = True,
            title: str = "",
            body: str = "",
            base: str = "",
            draft: bool = False,
            pr_number: int = 0,
        ) -> str:
            """Push an agent/* branch to GitHub and (by default) open a pull request with a clear body (## Summary, ## Changes, ## Tests, ## Caveats; the latest run_checks table is added if the body has no Tests section). To update an existing PR the user named, pass pr_number and its head branch. Explain the exact write in chat and wait for the user's clear reply before pushing. Opening a PR never merges it. If GitHub access is missing it saves a patch and returns status=blocked with the install link."""
            ctx.used_tools.append("push_branch")
            return dumps(await git_tools.push_branch(ctx, repo, branch, open_pr, title, body, base, draft, pr_number), 8000)

        from app.agent import dev_tools

        async def inspect(
            repo: str, action: str, ref: str = "", ref2: str = "", path: str = "", query: str = "",
            start: int = 0, end: int = 0, limit: int = 50, regex: bool = False,
        ) -> str:
            """Read-only git and code exploration in the local clone: status, branches, log, show, diff (ref..ref2), blame, grep, tree, read (line ranges). Prefer the composio-github read tools (get_tree, get_file, search_code, list_commits, compare_refs, blame_file) before cloning; use this once the repo is checked out."""
            ctx.used_tools.append("git_inspect")
            try:
                _, name = coding_mod.split_repo(repo)
                script = coding_mod.inspect_script(name, action, ref, ref2, path, query, start, end, limit, regex)
            except Exception as exc:  # noqa: BLE001
                return dumps({"error": str(exc)[:300]}, 1000)
            return dumps(await git_tools.sandbox_exec(ctx, script, 120), 20000)

        async def toolchain(extras: list[str] | None = None, force: bool = False) -> str:
            """Set up the headless IDE on the computer once: git, gh, ripgrep, fd, jq, tree, universal-ctags, node + npm/pnpm/yarn (corepack), python + uv + pytest, and git identity. Pass extras (go, java, rust) only when the repo needs them. Cached with a marker file, so calling it again is cheap. Returns what was installed and what failed."""
            ctx.used_tools.append("ensure_dev_toolchain")
            return dumps(await dev_tools.ensure_dev_toolchain(ctx, extras or [], force), 4000)

        async def overview(repo: str) -> str:
            """Understand a checked-out repo before editing: languages, frameworks, entry points, manifests, CI workflows, and the setup / lint / typecheck / unit / integration / build commands (from CI first, manifests second). Run this before your first edit."""
            ctx.used_tools.append("repo_overview")
            return dumps(await dev_tools.repo_overview(ctx, repo), 12000)

        async def symbol(repo: str, symbol: str, limit: int = 40) -> str:
            """Find where a function, class, type or variable is defined (universal-ctags, with a ripgrep fallback)."""
            ctx.used_tools.append("find_symbol")
            return dumps(await dev_tools.find_symbol(ctx, repo, symbol, limit), 12000)

        async def references(repo: str, symbol: str, path: str = "", limit: int = 80) -> str:
            """Find every usage of an identifier (whole word) with file:line, to judge the impact of a change."""
            ctx.used_tools.append("find_references")
            return dumps(await dev_tools.find_references(ctx, repo, symbol, path, limit), 16000)

        async def read_range(repo: str, path: str, start: int = 1, end: int = 200, ref: str = "") -> str:
            """Read lines start..end of a file with line numbers (optionally at a ref)."""
            ctx.used_tools.append("read_file_range")
            return dumps(await dev_tools.read_file_range(ctx, repo, path, start, end, ref), 32000)

        async def checks(repo: str, kinds: list[str] | None = None, commands: list | None = None, timeout_each: int = 600) -> str:
            """Test all cases: install deps, then run lint, type-check, unit tests, integration tests and the build (detected from CI workflows first, manifests second). On failure fix the code and run again, up to 3 attempts. The results go to the timeline and into the PR body."""
            ctx.used_tools.append("run_checks")
            cmds = [c.model_dump() if hasattr(c, "model_dump") else dict(c) for c in (commands or [])]
            return dumps(await dev_tools.run_checks(ctx, repo, kinds or [], cmds, timeout_each), 20000)

        async def review(
            repo: str, number: int, verdict: str = "COMMENT", summary: str = "", findings: list | None = None, include_checks: bool = True
        ) -> str:
            """Post a structured PR review through Composio GitHub: summary, findings by severity (inline comments where the line is in the diff, the rest in the body), the latest checks, and a verdict (COMMENT, REQUEST_CHANGES or APPROVE). Read the PR first (get_pull_request / compare_refs) and run checks on its branch when you can."""
            ctx.used_tools.append("review_pull_request")
            items = [f.model_dump() if hasattr(f, "model_dump") else dict(f) for f in (findings or [])]
            return dumps(await dev_tools.review_pull_request(ctx, repo, number, verdict, summary, items, include_checks), 4000)

        async def ask(question: str, options: list[str], waiting_for: str = "") -> str:
            """Ask the user one short question with 2-5 option buttons, only when something is genuinely ambiguous (which repo, a destructive step). Never ask them to configure, connect or install something: the tools sign in through a link themselves. Otherwise decide and proceed. End your turn after asking."""
            ctx.used_tools.append("ask_user")
            opts = [str(o).strip() for o in options or [] if str(o).strip()][:5]
            if not question.strip() or len(opts) < 2:
                return dumps({"error": "Give a question and 2-5 options."}, 500)
            ctx.question = {"question": question.strip()[:400], "options": opts}
            if waiting_for.strip():
                ctx.question["waiting_for"] = waiting_for.strip()[:40]
            return dumps({"asked": True, "note": "The user sees the options as buttons. End your turn now with the question in one line."}, 500)

        async def remember_coding(default_repo: str = "", branch_convention: str = "", base_branch: str = "", notes: str = "") -> str:
            """Save lasting coding preferences in your own memory: default repo, branch naming convention, usual base branch, notes. Repos you work in and PRs you open are remembered automatically."""
            from app.agent import coding_memory

            ctx.used_tools.append("remember_coding")
            prefs = {"default_repo": default_repo, "branch_convention": branch_convention, "base_branch": base_branch, "notes": notes}
            merged = await coding_memory.note(ctx, prefs={k: v for k, v in prefs.items() if v})
            return dumps({"saved": merged is not None, "memory": coding_memory.render(merged or {})}, 1500)

        from app.agent import deploy as deploy_mod

        def _params(params) -> dict:
            raw = params.model_dump() if hasattr(params, "model_dump") else dict(params or {})
            return {k: v for k, v in raw.items() if v not in ("", None) and not (k == "production" and v is True)}

        async def detect_targets(repo: str) -> str:
            """Find how this repo deploys (clones it first if needed): render.yaml, vercel.json, fly.toml, netlify.toml, railway.json/toml, serverless.yml, SAM, CDK, app.yaml, azure.yaml, Dockerfile. Without a config it detects the stack (Next.js, Vite, Node, FastAPI, Django, Flask, Go, static, Docker) and suggests a platform; no config is required to deploy. For each platform: connector, saved key, or CLI sign-in (handled by deploy)."""
            ctx.used_tools.append("detect_deploy_targets")
            return dumps(await deploy_mod.detect_deploy_targets(ctx, repo), 8000)

        async def deploy_tool(repo: str, platform: str = "", params=None, cwd: str = ".", use_cli: bool = False) -> str:
            """Deploy a repo to a platform, end to end: clones it if needed, installs the CLI, signs in (saved key, the CLI's session on the computer, or it starts the CLI's browser login and returns status=awaiting_user_login with the link: give the user just that link in one line and end the turn; the job resumes by itself), fills free-tier / India-region defaults, creates the service on first deploy (Render: from the GitHub repo), then clearly describe the platform, service, repo/branch, region and cost in chat and wait for the user's clear reply. After they say to continue, it waits until live, probes the URL and returns live_url, or the failure output: fix and call again (bounded retries reuse the confirmation). Never tell the user to configure or connect anything."""
            ctx.used_tools.append("deploy")
            return dumps(await deploy_mod.request_action(ctx, "deploy", repo, platform, _params(params or DeployParams()), cwd, use_cli), 8000)

        async def rollback_tool(repo: str, platform: str = "", params=None, cwd: str = ".", use_cli: bool = False) -> str:
            """Roll a deployment back to an earlier deploy / version / image. Explain the target in chat and wait for the user's clear reply first. Find the target with deploy_status."""
            ctx.used_tools.append("deploy_rollback")
            return dumps(await deploy_mod.request_action(ctx, "rollback", repo, platform, _params(params or DeployParams()), cwd, use_cli), 8000)

        async def deploy_status_tool(platform: str, repo: str = "", params=None, cwd: str = ".") -> str:
            """Read-only: recent deploys / revisions / services on a platform (no approval needed). Use to find service ids, deploy ids for rollback, and whether a deploy is live."""
            ctx.used_tools.append("deploy_status")
            return dumps(await deploy_mod.read_action(ctx, "status", repo, platform, _params(params or DeployParams()), cwd), 12000)

        async def deploy_logs_tool(platform: str, repo: str = "", params=None, cwd: str = ".") -> str:
            """Read-only: recent logs for a service / deploy on a platform (no approval needed). Use after a failed deploy to find the cause."""
            ctx.used_tools.append("deploy_logs")
            return dumps(await deploy_mod.read_action(ctx, "logs", repo, platform, _params(params or DeployParams()), cwd), 12000)

        async def gen_config_tool(repo: str, platform: str = "", cwd: str = ".", write: bool = True) -> str:
            """Generate a deploy config for a repo that has none (render.yaml, vercel.json, netlify.toml, fly.toml, railway.json, Dockerfile) from its detected stack, with free-tier and India-region defaults. Writes into the working tree without overwriting; commit it with git_commit + push_branch (approval) to put it in a PR. No approval needed to generate."""
            ctx.used_tools.append("generate_deploy_config")
            return dumps(await deploy_mod.generate_config(ctx, repo, platform, cwd, write), 8000)

        tools.extend(
            [
                StructuredTool.from_function(coroutine=detect_targets, name="detect_deploy_targets", description=detect_targets.__doc__, args_schema=DetectDeployInput),
                StructuredTool.from_function(coroutine=gen_config_tool, name="generate_deploy_config", description=gen_config_tool.__doc__, args_schema=GenerateDeployConfigInput),
                StructuredTool.from_function(coroutine=deploy_tool, name="deploy", description=deploy_tool.__doc__, args_schema=DeployInput),
                StructuredTool.from_function(coroutine=rollback_tool, name="deploy_rollback", description=rollback_tool.__doc__, args_schema=DeployInput),
                StructuredTool.from_function(coroutine=deploy_status_tool, name="deploy_status", description=deploy_status_tool.__doc__, args_schema=DeployReadInput),
                StructuredTool.from_function(coroutine=deploy_logs_tool, name="deploy_logs", description=deploy_logs_tool.__doc__, args_schema=DeployReadInput),
            ]
        )
        tools.extend(
            [
                StructuredTool.from_function(coroutine=coding_status, name="coding_workspace_status", description=coding_status.__doc__),
                StructuredTool.from_function(
                    coroutine=shell, name="sandbox_shell", description=shell.__doc__, args_schema=SandboxShellInput
                ),
                StructuredTool.from_function(coroutine=checkout, name="git_checkout", description=checkout.__doc__, args_schema=GitCheckoutInput),
                StructuredTool.from_function(coroutine=tests, name="run_tests", description=tests.__doc__, args_schema=RunTestsInput),
                StructuredTool.from_function(coroutine=diff, name="git_diff", description=diff.__doc__, args_schema=GitDiffInput),
                StructuredTool.from_function(coroutine=commit, name="git_commit", description=commit.__doc__, args_schema=GitCommitInput),
                StructuredTool.from_function(coroutine=push, name="push_branch", description=push.__doc__, args_schema=PushBranchInput),
                StructuredTool.from_function(coroutine=inspect, name="git_inspect", description=inspect.__doc__, args_schema=GitInspectInput),
                StructuredTool.from_function(coroutine=toolchain, name="ensure_dev_toolchain", description=toolchain.__doc__, args_schema=ToolchainInput),
                StructuredTool.from_function(coroutine=overview, name="repo_overview", description=overview.__doc__, args_schema=RepoOnlyInput),
                StructuredTool.from_function(coroutine=symbol, name="find_symbol", description=symbol.__doc__, args_schema=SymbolInput),
                StructuredTool.from_function(coroutine=references, name="find_references", description=references.__doc__, args_schema=ReferencesInput),
                StructuredTool.from_function(coroutine=read_range, name="read_file_range", description=read_range.__doc__, args_schema=ReadRangeInput),
                StructuredTool.from_function(coroutine=checks, name="run_checks", description=checks.__doc__, args_schema=RunChecksInput),
                StructuredTool.from_function(coroutine=review, name="review_pull_request", description=review.__doc__, args_schema=ReviewInput),
                StructuredTool.from_function(coroutine=remember_coding, name="remember_coding", description=remember_coding.__doc__, args_schema=RememberCodingInput),
            ]
        )
        if ctx.job is None or (getattr(ctx.job, "depth", 1) or 1) <= 1:
            # Only the user-facing level asks questions: chat turns and root jobs (children report to their parent).
            tools.append(StructuredTool.from_function(coroutine=ask, name="ask_user", description=ask.__doc__, args_schema=AskUserInput))
        if ctx.job is None and ctx.db is not None and (ctx.teammate_depth == 0 or coding_mod.is_coding_agent(ctx.agent)):
            # Coding teammates reached through ask_teammate (group chats) may start jobs too, so long work (and
            # waiting for a sign-in) runs in the background instead of inline.

            async def start_job(task: str, title: str = "") -> str:
                """Start a background coding job for multi-step work (code changes, fixes, features, reviews, merges, deploys). It runs on the computer without the chat timeout and shows a live timeline (todo list, steps, tests, PR links). If user input is needed, ask in natural language and accept a conversational reply in this chat. Give a self-contained task: repo (owner/name), goal and acceptance criteria. Follow-ups in this chat continue the same job."""
                ctx.used_tools.append("start_coding_job")
                return dumps(await start_coding_job(ctx, task, title), 4000)

            tools.append(
                StructuredTool.from_function(
                    coroutine=start_job, name="start_coding_job", description=start_job.__doc__, args_schema=StartJobInput
                )
            )
    if ctx.job is None and ctx.db is not None and ctx.agent is not None and ctx.teammate_depth == 0:

        async def start_task(task: str, title: str = "") -> str:
            """Move long work to the background: tasks with many steps or pages (multi-site comparisons, research across many sources, long computer sessions, anything likely to take more than a couple of minutes). It runs without the chat timeout, survives the user closing the chat or the app restarting, and shows a live timeline (plan, steps, results) under this reply. Give a self-contained task: goal, constraints, what to deliver. Quick lookups stay in this turn."""
            ctx.used_tools.append("start_background_task")
            return dumps(await start_coding_job(ctx, task, title, role="worker"), 4000)

        tools.append(
            StructuredTool.from_function(
                coroutine=start_task, name="start_background_task", description=start_task.__doc__, args_schema=BackgroundTaskInput
            )
        )
    if ctx.job is not None and ctx.teammate_depth == 0 and ctx.db is not None:
        from app.jobs.service import MAX_DEPTH

        if (getattr(ctx.job, "depth", 1) or 1) < MAX_DEPTH:

            async def delegate_tool(role: str, task: str, agent_name: str = "", title: str = "") -> str:
                """Hand a step of this background job to a child job: role 'coder' builds and opens the PR, 'reviewer' reviews a PR and comments, 'planner' splits a large piece further. The child runs after this turn; you resume with its result. Give a self-contained task (repo, branch, PR number, acceptance criteria). End your turn after delegating."""
                ctx.used_tools.append("delegate")
                return dumps(await delegate_job(ctx, role, task, agent_name, title), 4000)

            tools.append(
                StructuredTool.from_function(
                    coroutine=delegate_tool, name="delegate", description=delegate_tool.__doc__, args_schema=DelegateInput
                )
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
    tools.extend(
        [
            StructuredTool.from_function(
                coroutine=save_skill,
                name="save_skill",
                description=save_skill.__doc__,
                args_schema=SaveSkillInput,
            ),
            StructuredTool.from_function(
                coroutine=list_my_skills,
                name="list_my_skills",
                description=list_my_skills.__doc__,
            ),
            StructuredTool.from_function(
                coroutine=get_skill,
                name="get_skill",
                description=get_skill.__doc__,
                args_schema=GetSkillInput,
            ),
            StructuredTool.from_function(
                coroutine=skill_progress,
                name="skill_progress",
                description=skill_progress.__doc__,
                args_schema=SkillProgressInput,
            ),
            StructuredTool.from_function(
                coroutine=start_teaching,
                name="start_teaching",
                description=start_teaching.__doc__,
            ),
            StructuredTool.from_function(
                coroutine=stop_teaching,
                name="stop_teaching",
                description=stop_teaching.__doc__,
                args_schema=StopTeachingInput,
            ),
        ]
    )
    return tools


def _fast_browser_hint() -> str:
    try:
        from app.agent import fast_browser

        return fast_browser.prompt_hint()
    except Exception:  # noqa: BLE001
        return ""


def shell_routing_hint(connectors: list | None, agent=None) -> str:
    """Prompt lines that route installs/commands to the cloud computer (docs/COMPUTER.md)."""
    from app.agent.coding import is_coding_agent
    from app.agent.computer_harness import LOCAL_PROVIDERS

    local = any(getattr(c, "provider", "") in LOCAL_PROVIDERS for c in (connectors or []))
    text = (
        "Commands, installs and CLIs ('install X', 'run Y', 'set up the requirements') run on the cloud computer with "
        "shell, which every agent has. If a runtime or CLI is missing (node/npm, pnpm, python/pip, gh, vercel, render, "
        "...), install it yourself (ensure_toolchain, or shell: npm -g, pip --user, ~/.local/bin; shell also auto-installs "
        "known tools on 'command not found') and carry on. Never tell the user to install something on their own "
        "machine unless they asked you to use their own machine. "
    )
    if local:
        text += (
            "user_computer_shell runs on the user's own connected computer (with their confirmation): use it only when "
            "the user explicitly asks for their own machine. "
        )
    else:
        text += (
            "No local computer is connected, so everything runs on the cloud computer; if the user explicitly wants "
            "their own laptop, say it needs the Checkme local agent connected and offer to do it in the cloud now. "
        )
    if not is_coding_agent(agent):
        text += (
            "Repo changes, pull requests, merges and deploys belong to a coding agent: hand them over (ask_teammate a "
            "coding teammate in a group, or delegate with role='coder' and agent_name of a coding agent inside a background job) instead of refusing; "
            "merges and deploys always wait for the user's approval."
        )
    return text.strip()


def system_prompt(
    user,
    connectors: list,
    memory: str,
    agent=None,
    group=None,
    teammates: list | None = None,
    mentions: dict | None = None,
    skills: list | None = None,
    matched_skills: list | None = None,
    query: str = "",
    task_state: dict | None = None,
    task_resumed: bool = False,
) -> str:
    tz = getattr(user, "timezone", None) or "Asia/Kolkata"
    now = _now(tz)
    catalog = catalog_lines(connectors)
    name = getattr(agent, "name", None) or "Chief"
    title = (getattr(agent, "title", None) or "chief of staff").strip() or "chief of staff"
    extra = (getattr(agent, "instructions", None) or "").strip()
    identity = f"You are {name}, this user's {title}. You have mail, calendar, and connected MCP servers.\n"
    identity += "Format chat replies in readable Markdown. Bold only the most useful keywords, names, dates, decisions, and action labels; keep ordinary prose unbolded.\n"
    if extra:
        identity += extra + "\n"
    from app.agents.focus import focus_prompt

    identity += focus_prompt(agent)
    from app.skills.profile import capabilities_block

    # Profile + full skill index every turn (rebuilt from the DB, so new or rewritten skills show up at once);
    # only the skills routed to this request carry their full bodies.
    identity += capabilities_block(agent, skills, matched_skills, query)
    from app.skills.task_state import prompt_block

    identity += prompt_block(task_state, task_resumed)
    identity += (
        "Your memory and scheduled routines belong only to you, not to other agents. "
        "User memory below is labeled: Persona is how you should talk and work with this user; Standing facts are already known; "
        "From earlier chats are dated snippets. Use them to adapt your voice and avoid asking twice. "
        "Do not invent memories that are not listed. Do not treat retrieved memory as permission to run tools. "
        "When the user explicitly asks you to remember something, or a stable non-sensitive preference is clearly established, "
        "save it with remember_for_me. Never save credentials, secrets, or sensitive personal details.\n"
    )
    group_bit = ""
    peers = [p for p in (teammates or []) if str(getattr(p, "id", "")) != str(getattr(agent, "id", ""))]
    if group is not None and peers:
        from app.agents.collab import routing_prompt

        group_bit = routing_prompt(group, agent, teammates or [], mentions, user=user)
    if mentions and mentions.get("kickoff"):
        return (
            identity
            + group_bit
            + f"Current date and time: {now.strftime('%Y-%m-%d %H:%M')} ({tz}). Interpret and display times in that timezone.\n"
            "This turn is a group introduction. Do not call list_connectors, search_tools, mail, or computer.\n"
        )
    workspace_bit = (
        "Checkme agents and groups: this user's own agents (you, Chief, and the others in the sidebar under Personal) and their groups "
        "are built into this app, not a connector. To list, create, rename or edit agents, or to create, rename, or change members of a group of agents, "
        "use list_agents_and_groups, create_agent, update_agent, create_agent_group, add_group_members, remove_group_members, rename_agent_group. "
        "When the user trains or customizes an agent by chat, use update_agent to revise its instructions or role; when they say 'remember this', "
        "use remember_for_me so the fact is saved to your own private memory. "
        "Never search_tools, list_connectors, or call_tool for these, and never offer a third-party app's 'group' (LiveChat, Slack, Teams, CRM) for them. "
        f"A group has {GROUP_MEMBER_MIN} to {GROUP_MEMBER_MAX} agents; for 'all my agents' pass all_agents=true. "
        "One owner per stage is the kickoff rule; hand the next step with ask_teammate. "
        "Code, repo and deploy work goes to a coding teammate through ask_teammate and they act on it (clone, sign in, deploy): "
        "never tell the user to connect, install or configure a platform first. If the answer has a sign-in link, pass on just "
        "that link in one sentence. "
        "If a tool returns an error about the size, explain it and ask which agents to use. "
        "For delete_agent and delete_agent_group, explain what will be removed in conversational language and wait for a clear reply. "
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
        "list_connectors includes each connected marketplace app (Figma, Slack, …) plus composio. "
        "search_tools expands those tools; list_connections also shows which apps are authorized. "
        "If the user names a connected app, search_tools then call_tool on composio. Do not say it is unavailable. "
        "To connect an app the user named, call connect_app with that toolkit slug (gmail, github, figma, slack) — never a different vendor. "
        "connect_app returns redirect_url; the chat always shows the Composio connect card. Do not only paste the URL, and do not refuse to connect when composio is listed.\n"
        "Computer, browser, files, web search, memory, skills, and scratchpad are first-class tools. Call them directly. "
        "Do not search_tools or call_tool for browser_navigate, browser_snapshot, browser_act, computer_observe, computer_act, "
        "shell, run_code, list_files, read_file, write_file, attach_file, open_path, launch_app, request_takeover, web_search, web_read, web_fetch, "
        "remember, recall_memory, or scratchpad_add.\n"
        "When the user asks for a chart, plot, HTML report, or downloadable file: fetch the data first, then "
        "render_plot or write_analytics_report with tables and charts, or write_file plus attach_file / publish_file. "
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
        "2) Anything the user could do on a public website in a normal browser (searching, comparing, reading pages, "
        "checking live prices, schedules or availability, profiles, 'just check', 'look up', 'open her/his page'): "
        "open the computer browser even if a marketplace app exists. If that app is not in list_connections, do not ask to connect it for a lookup. "
        "browser_navigate Google or the named site so the computer screen shows the page. "
        "Do not search_tools for Instagram/Twitter/Facebook unless list_connections already shows that app.\n"
        "3) A named website or URL, or a page you must click or fill: the computer browser. "
        "Use a connected data tool first only when list_connections shows that app is authorized.\n"
        "This user's computer is one shared live machine: shell/files, browser sessions, and logins. "
        "Each bot has its own screen on that machine. Only one computer-use task may run at a time. "
        f"{shell_routing_hint(connectors, agent)}\n"
        "Public facts, news, and 'what is going on': web_search first; its top results already include page text "
        "(and often an `answer`), so answer from them. If you need more, read the best 2-3 URLs in ONE web_read call "
        "(parallel, server-side). Quick questions never need the computer. "
        "If search is empty, browser_navigate a Google search URL with the user's words "
        "(https://www.google.com/search?q=...) then open at least two result links. Search the acronym; do not ask what it means first. "
        "If navigate returns chrome-error, chromewebdata, or connection reset, web_fetch the same URL (or another source) and keep going. "
        "web_fetch and web_read read pages server-side and return text (they do not use or wake the computer). To show, "
        "click or fill a page (JavaScript apps such as maps and listing sites, logins, forms), browser_navigate it on the "
        "computer, then browser_snapshot and browser_act on it. "
        "web_search already falls back to searching in the computer browser; browser_search does that directly. web_search tries Tavily (and any other configured search API) first, then the computer browser, then free server-side search engines; Tavily often returns each result's page text, so web_fetch or web_read on a result is instant. If it returns `setup`, tell the user in one line which search key to configure (it names the env vars) and why, then keep going with browser_navigate on known sites. "
        "Never stop after one failed page. Never say you cannot verify live updates until you have searched and opened at least one article.\n"
        "Browser operator: execute the user's website task on the live computer yourself. Do not tell them how to click. "
        "Loop until done: inspect the snapshot (tree, verified, alerts, tables) → browser_act one logical step → read the new snapshot → continue. "
        f"{_fast_browser_hint()}"
        "Identify controls by role and name first, then label, then visible text; computer_act x,y last. "
        "Fill whole forms from the snapshot: one fill per ordinary field. Comboboxes and type-ahead lists "
        "(airports, places, search suggestions): type or select/pick with that field's ref and the value — do not fill, and do not "
        "batch several type-ahead fields in one browser_act; pick each matching option before the next field. If the option has no "
        "ref, computer_observe then computer_act (click the suggestion, or press ArrowDown then Enter). Never request_takeover to "
        "pick a dropdown option. Dates, checkboxes, radios, and page file pickers are your job (upload with path= a computer file; "
        "download saves the file). When a download (or publish_file) returns extracted_text, analyze it in YOUR role — your title, "
        "standing instructions, focus, and skills — not as a generic summarizer. If a listed skill covers this file or domain, get_skill "
        "and follow it before you write the analysis. Post findings, figures, risks, and recommended actions that fit that domain in the "
        "chat reply, and cite download_url. If the file is tabular or numeric and your role includes data, sales, inventory, finance, or "
        "reporting, also call write_analytics_report with the real figures. When the user asks for a deep dive, or extracted_text is truncated, "
        "incomplete, or too coarse for the question, run_code on the saved download path (pandas; install with pip --user if missing), print "
        "real computed figures, then write_analytics_report or attach_file any chart you wrote under /tmp. Do not invent numbers. Do not only drop a link. "
        "If extracted_text is empty, say you attached the file and its kind (image/pdf/zip) and still give download_url. "
        "After each critical step check verified.alerts and that values stuck; retry once with another strategy if they did not. "
        "Do not invent personal, address, financial, or ID data — ask only for facts you do not have. "
        "Search, filter, fill drafts, and download are allowed. Do not click Submit, Pay, Book, Send, Publish, or Delete unless the user clearly asked you to finish that step. "
        "If a page tool returns fallback computer_act, call computer_observe then computer_act (click x,y / type / key / scroll). "
        "Prefer the destination site (put dates and filters in that site's URL when the user gave them) over a Google Search results page for live results, prices, availability, or forms — Google Search often returns a verification wall to the cloud browser. "
        "Cite only what is visible on the snapshot. If the page has no prices, times, or results yet, say so. Never treat a generic landing page as a confirmed result. "
        "Blocked sites: browser_navigate and web_fetch already wait out passive bot checks and, when a site still blocks, try other ways to read the same page (Tavily's copy, a proxy or hosted browser if configured, the site's mobile/AMP version, a reader, an archived copy). A result with read_only=true is such a copy: use it, cite it, and say it is read-only (and archived/dated when `archived` is set). "
        "If a page comes back blocked (blocked_source, bot_wall with handoff=false, 'Access Denied'), do not say the browser is restricted: tell the user plainly which site blocked automated access (for example 'Myntra blocked automated access') and continue with equivalent sources for the same information (other retailers, the brand or insurer's own site, comparison and review sites, news). Never invent what a blocked page says. "
        "If the page is a CAPTCHA that only a person can solve (bot_wall with handoff=true, or a 'press and hold' / picture puzzle): first get the answer from other sites if that is enough; when the user needs that exact site (to log in, buy, or see their own data), request_takeover so they solve it on the live screen, and do not invent numbers. "
        "Login, 2FA, captcha, or payment: request_takeover (same as browser_handoff) with a short instruction so the user takes control of that same screen. After they finish, snapshot and continue. Do not hand over for dropdowns, autocomplete, dates, or other page controls — keep acting until the form is filled. "
        "If a browser tool says it failed to start or a module is missing after the user used the computer, the live desktop still has their work: snapshot or observe and continue the remaining steps. Never declare the job failed because the page browser restarted. "
        "Never attach page.png in chat; the computer screen already shows the tab. "
        "Cheapest path first: a connector or API tool, then web_search + web_fetch; switch to the computer browser when "
        "the answer needs interaction (search forms, dates, filters, logins, pages that only render in a browser) or the cheap "
        "path came back empty or blocked. On the computer run a tight loop: act, read the returned snapshot (each "
        "browser_navigate/browser_act returns a fresh tree with refs), decide, act again, until the task is done. "
        "Refs (e0, e1) are only valid for the latest snapshot; after a page changes use the new ones. "
        "If an action returns ok=false, read its error and the fresh snapshot, then adjust instead of stopping. "
        "Long work (many pages or sites, or likely more than a couple of minutes) goes to start_background_task when "
        "it is offered: start it, reply in a line or two, and let the job do the work.\n"
        "Every agent has the computer: when a task needs a website without an API, use it and carry the task through "
        "step by step (navigate, search, click, type, read, repeat) instead of describing how the user could do it. "
        "The computer boots on demand; the first call may take a minute. If a computer tool returns an error, try once "
        "more if it looks transient, then tell the user that exact error (for example an invalid E2B key, a quota limit "
        "or a boot timeout). Never say the computer or browser is unavailable unless a computer tool returned that error "
        "in this turn. "
        "Memory: remember (or save_memory) only when the user asked to remember something or stated a lasting preference. "
        "recall_memory to look it up, forget_memory to delete it, save_shared_memory for facts every agent should know (name, city, timezone). "
        "Do not dump the whole chat into memory. Durable facts are also extracted after the turn. "
        "search_history / read_history for this chat's messages.\n"
        "Scratchpad: scratchpad_add a short plan for multi-step work in this chat, then complete items as you go.\n"
        "Skills: your profile lists every skill; get_skill loads one in full before you follow it, list_my_skills lists them. "
        "skill_create / save_skill for a repeatable workflow, skill_update / skill_delete to manage them, "
        "or start_teaching so the user demonstrates on the visible screen "
        "(up to ten minutes; they can turn on the microphone in the screen panel to narrate), then stop_teaching with a name.\n"
        "task_catalog action=search to find briefing, inbox, schedule, coding-job, or media tasks, then load and execute.\n"
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
        "For event-driven work, create a webhook automation with run_staff_tool(name='create_automation', args={trigger_type:'webhook', name, trigger:<exact event name>, condition, action, requires_approval:true}). "
        "Keep external actions gated on a clear user reply. When a tool says a confirmation is needed, tell the user in one natural sentence exactly what you prepared and ask whether they want you to go ahead; do not mention approval, approve, cards, or buttons, and wait for a clear reply. Return the generated webhook URL, one-time secret, exact headers, and a ready-to-copy curl example; the secret is only returned at creation. List or disable automations with the automation endpoints when asked. "
        "If they want a calendar invite or meeting at a clock time, that time is the event start: call create_event now. "
        "If repeated requests reveal a useful routine, point that out and offer to schedule it; create it when the user asks. Use an existing schedule or webhook "
        "for future follow-through, and keep approval enabled for external actions unless the user explicitly changes it. "
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
    if bool(getattr(s, "stream_tokens", True)):
        # Tokens reach the callbacks as they are generated (the chat shows the reply as it streams).
        base["streaming"] = True
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
    from_name = getattr(ctx.agent, "name", None) or "a teammate"
    group_name = getattr(ctx.group, "name", None) or "group"
    nested_query = (
        f"You are posting in the group '{group_name}' as yourself. "
        f"The user said: {clip(ctx.query, 500)}\n"
        f"{from_name} asked you: {task}"
    )
    peer_key = f"peer:{ctx.task_state['id']}:{member.id}" if ctx.task_state else None
    handed = await _hand_over_state(ctx, member.id, member.name, peer_key) if peer_key else None
    nested = await run_deep_agent(
        ctx.db,
        ctx.user,
        ctx.session_id,
        ctx.request_id or "peer",
        [],
        nested_query,
        task_key=peer_key,
        auto_confirm=ctx.auto_confirm,
        workspace_agent=member,
        group=ctx.group,
        teammates=ctx.teammates,
        teammate_depth=ctx.teammate_depth + 1,
        mentions={"handoff": True},
        progress=ctx.progress.child(member.name) if ctx.progress is not None else None,
    )
    ctx.used_tools.append(f"ask_teammate:{member.name}")
    if handed:
        await _take_back_state(ctx, handed["id"], nested.get("text") or "", member.name)
    for item in nested.get("attachments") or []:
        ctx.attachments.append(item)
    for name in nested.get("used_tools") or []:
        ctx.used_tools.append(str(name))
    if nested.get("workspace_changed"):
        ctx.workspace_changed = True
    for item in nested.get("pending_confirms") or []:
        ctx.pending.append(item)
    for info in nested.get("jobs_started") or []:
        ctx.jobs_started.append(info)
    if nested.get("question") and ctx.question is None:
        ctx.question = nested["question"]
    for view in nested.get("browser") or []:
        ctx.results.append(
            {
                "result_id": f"r{len(ctx.results) + 1}",
                "tool_name": "browser_navigate",
                "data": view if isinstance(view, dict) else {},
            }
        )
    item = {
        "text": nested.get("text") or "",
        "agent_id": str(member.id),
        "agent_name": member.name,
        "agent_color": getattr(member, "color", "") or "",
        "agent_avatar": getattr(member, "avatar", "") or "",
        "peer": True,
        "used_tools": nested.get("used_tools") or [],
        "computer": nested.get("computer") or {},
        "attachments": nested.get("attachments") or [],
    }
    ctx.thread.append(item)
    return {"from": member.name, "agent_id": str(member.id), "reply": item["text"]}


async def _route_and_confirm(all_skills: list, query: str) -> list:
    """Skills that fit this request: routed (keywords + embeddings), then confirmed by a short LLM check."""
    from app.skills import task_state as ts
    from app.skills.profile import route_skills

    matched = await route_skills(all_skills, query)
    if matched:
        matched = await ts.confirm_skills(query, matched)
    return matched


def _progress_callbacks(ctx: RunContext, conns: list, query: str) -> list:
    if ctx.progress is None:
        return []
    from app.agent.progress import ProgressCallback

    names = {str(c.slug): str(getattr(c, "alias", "") or getattr(c, "name", "") or c.slug) for c in conns}
    agents = {str(getattr(p, "id", "")): str(getattr(p, "name", "")) for p in ctx.teammates}
    return [ProgressCallback(ctx.progress, query=query, connector_names=names, agent_names=agents)]


async def with_computer(db: AsyncSession, user, conns: list) -> list:
    """Every agent gets the user's computer whenever a computer backend (E2B or Daytona) is configured.

    ``user_connectors`` drops rows whose health is not ok/pending (an old Daytona failure could leave the computer
    row 'down'), which used to make browser tools answer "the computer is not available for this agent" even though
    the desktop boots on demand. The computer's own boot reports real errors (invalid key, quota, timeout) instead.
    """
    from app.agent.coding import COMPUTER_SLUG
    from app.config import get_settings

    if db is None or not get_settings().computer_backend:
        return conns
    if any(getattr(c, "slug", "") == COMPUTER_SLUG for c in conns):
        return conns
    try:
        row = (
            await db.execute(select(Connector).where(Connector.user_id == user.id, Connector.slug == COMPUTER_SLUG))
        ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        log.warning("computer connector lookup failed: %s", exc)
        return conns
    if row is None:
        return conns
    log.info(
        "computer connector added for this run (enabled=%s health=%s)", getattr(row, "enabled", None), getattr(row, "health", None)
    )
    return [*conns, row]


async def _begin_task_state(ctx: RunContext, skill: dict | None, meta_question: bool = False, resumed_ok: bool = True) -> None:
    """Open or resume this task's skill state and narrate it (chat status line + job timeline)."""
    from app.skills import task_state as ts

    if ctx.db is None or not ctx.task_key:
        return
    try:
        state, resumed = await ts.begin(
            ctx.user,
            ctx.task_key,
            agent_id=getattr(ctx.agent, "id", None),
            skill=skill,
            session_id=ctx.session_id,
            job_id=getattr(ctx.job, "id", None),
            meta_question=meta_question,
        )
    except Exception:  # noqa: BLE001 - never block a turn on task state
        log.info("task state: begin failed", exc_info=True)
        return
    ctx.task_state = state
    ctx.task_resumed = bool(resumed and resumed_ok)
    if state:
        if not any(str(s.get("id")) == str(state.get("skill_id")) for s in ctx.active_skills):
            ctx.active_skills.append({"id": state.get("skill_id"), "name": state.get("skill_name"), "match": {"by": "resumed" if resumed else "loaded"}})
        await _narrate_task_state(ctx, "", resumed=resumed)


async def _narrate_task_state(ctx: RunContext, note: str = "", resumed: bool = False) -> None:
    from app.skills import task_state as ts

    state = ctx.task_state
    if not state or ctx.progress is None:
        return
    verb = "Resuming skill" if resumed else "Using skill"
    line = f"{verb}: {state.get('skill_name')} · {ts.step_label(state)}" + (f" — {clip(note, 160)}" if note else "")
    if hasattr(ctx.progress, "record"):  # job timeline: a status event carrying the state
        await ctx.progress.record("status", line, {"skill_state": ts.serialize(state)})
    else:  # chat: the live status line
        await ctx.progress.emit(line, kind="skill")


async def _hand_over_state(ctx: RunContext, to_agent_id, to_name: str, task_key: str, job_id=None) -> dict | None:
    """Pass this task's skill state to another agent (delegate / ask_teammate). Returns the child state."""
    from app.skills import task_state as ts

    if not ctx.task_state or not task_key:
        return None
    try:
        child = await ts.handover(ctx.task_state, to_agent_id, task_key, by=getattr(ctx.agent, "name", "") or "", to_name=to_name, job_id=job_id)
        ctx.task_state = await ts.STORE.get(ctx.task_state["id"]) or ctx.task_state
    except Exception:  # noqa: BLE001
        log.info("task state: handover failed", exc_info=True)
        return None
    if ctx.progress is not None:
        line = f"Handing skill {child.get('skill_name')} ({ts.step_label(child)}) to {to_name}"
        if hasattr(ctx.progress, "record"):
            await ctx.progress.record("status", line, {"skill_state": ts.serialize(child)})
        else:
            await ctx.progress.emit(line, kind="skill")
    return ts.serialize(child)


async def _take_back_state(ctx: RunContext, child_id, result: str, by: str) -> None:
    """A teammate finished its part in this turn: close its state and write the result back to ours."""
    from app.skills import task_state as ts

    try:
        await ts.finish(child_id, result, ok=True, by=by)
        if ctx.task_state:
            ctx.task_state = await ts.STORE.get(ctx.task_state["id"]) or ctx.task_state
    except Exception:  # noqa: BLE001
        log.info("task state: taking back the handover failed", exc_info=True)
        return
    if ctx.task_state:
        await _narrate_task_state(ctx, f"result back from {by}")


def _task_state_out(ctx: RunContext) -> dict | None:
    from app.skills import task_state as ts

    return ts.serialize(ctx.task_state)


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
    mentions: dict | None = None,
    progress: Any = None,
    job: Any = None,
    task_key: str | None = None,
) -> dict[str, Any]:
    from app.connectors.platform import ensure_platform_connectors
    from app.debug_ndjson import dbg as _dbg_start

    # #region agent log
    _dbg_start("C", "deep.py:run_deep_agent", "agent start", {"query": (query or "")[:80], "n_history": len(history or [])})
    # #endregion
    import time as _time

    from app.agent import fast_path
    from app.agent.coding import is_coding_agent as _coding_agent

    timer = fast_path.TurnTimer()
    plan = fast_path.classify_turn(
        query,
        has_uploads="attachment_key=" in (query or ""),
        confirmations=bool(confirmations),
        coding_agent=_coding_agent(workspace_agent),
    )
    if not getattr(get_settings(), "fast_path_enabled", True) or teammate_depth or job is not None:
        plan = fast_path.TurnPlan(fast_path.DEEP, "fast path off for this run")
    timer.kind = plan.kind
    if progress is not None and not teammate_depth:
        # Something visible right away, before connectors, memory and skills load.
        await progress.emit("Looking this up on the web" if plan.kind == fast_path.QUICK_WEB else "Reading your message", kind="start")
        timer.status()
    _t = _time.monotonic()
    await ensure_platform_connectors(db, user)
    conns = await with_computer(db, user, await user_connectors(db, user.id))
    assignments = await connector_assignment_map(db, user)
    toolkits = await toolkit_assignment_map(db, user) if db is not None else {}
    teammate_ids = [getattr(peer, "id", None) for peer in (teammates or [])]
    conns = filter_connectors(
        conns,
        getattr(workspace_agent, "id", None),
        teammate_ids,
        assignments,
    )
    mentions = dict(mentions or {})
    if teammate_depth:
        mentions["handoff"] = True
        mentions.pop("kickoff", None)
        mentions.pop("roll_call", None)
        mentions.pop("everyone", None)
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
        job=job,
        toolkit_assignments=toolkits,
    )
    if plan.kind == fast_path.COMPUTER:
        # Start the browser (and restore saved sign-ins) while the model plans its first step.
        try:
            from app.agent import fast_browser

            fast_browser.prewarm(ctx)
        except Exception:  # noqa: BLE001
            log.info("browser prewarm could not start", exc_info=True)
    await _maybe_start_connect(ctx, query)
    from app.agent import scratchpad as pad

    await pad.load(ctx)
    timer.mark("setup", _t)
    from app.agent import cancel as run_cancel

    callbacks = _progress_callbacks(ctx, conns, query) + [fast_path.TimingCallback(timer)]
    if run_cancel.current.get() is not None:
        callbacks.append(run_cancel.callback())
    all_skills: list = []
    skills_task = None
    if db is not None:
        # Skill routing (embeddings + a confirm call) needs no DB once the index is loaded: run it while memory loads.
        from app.skills.service import list_skills

        _t = _time.monotonic()
        try:
            all_skills = await list_skills(db, user, getattr(workspace_agent, "id", None))
        except Exception:  # noqa: BLE001
            log.info("skills: loading the skill index failed", exc_info=True)
            await db.rollback()
        skills_task = asyncio.create_task(_route_and_confirm(all_skills, query))
        timer.mark("skills_index", _t)
    if progress is not None and plan.kind != fast_path.QUICK_WEB:
        await progress.emit("Recalling what I know about this" if not teammate_depth else "Picking up the task", kind="memory")
    _t = _time.monotonic()
    memory = await load_memory(db, user, query, workspace_agent)
    timer.mark("memory", _t)
    pad_bit = pad.as_prompt(ctx.scratchpad)
    if pad_bit:
        memory = ((memory or "").rstrip() + "\n\n" + pad_bit).strip()
    from app.agent.coding import is_coding_agent as _is_coder

    if _is_coder(workspace_agent):
        from app.agent import coding_memory

        block = await coding_memory.read(db, user, workspace_agent)
        if block and block not in (memory or ""):
            memory = ((memory or "").rstrip() + "\n\nYour coding memory (repos, conventions, past PRs):\n" + block).strip()
    extra = []
    matched_skills: list = []
    if db is not None:
        _t = _time.monotonic()
        try:
            matched_skills = await skills_task if skills_task is not None else []
        except Exception:  # noqa: BLE001
            log.info("skills: routing failed", exc_info=True)
            matched_skills = []
        timer.mark("skills_route", _t)
        from app.skills import task_state as ts
        from app.skills.profile import is_meta_question

        ctx.active_skills = [{"id": s.get("id"), "name": s.get("name"), "match": s.get("match")} for s in matched_skills]
        ctx.task_key = task_key or ts.task_key_for(
            session_id=session_id, agent_id=getattr(workspace_agent, "id", None), job_id=getattr(job, "id", None)
        )
        await _begin_task_state(
            ctx, matched_skills[0] if matched_skills else None, meta_question=is_meta_question(query)
        )
        if matched_skills and progress is not None and not ctx.task_state:
            names = ", ".join(str(s.get("name")) for s in matched_skills)
            await progress.emit(f"Skill for this task: {names}", kind="skill")
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
        from app.agent.coding import CODING_SLUG

        if slug == CODING_SLUG:
            from app.agent import git_tools

            result = await git_tools.run_confirmed(ctx, tool, payload.get("args") or {})
            ctx.used_tools.append(tool)
            extra.append(
                HumanMessage(
                    content=f"The user approved {tool}. Result: {clip(dumps(result, 4000), 4000)}. Tell the user what happened and share any PR or deploy link."
                )
            )
            continue
        result = await call_user_tool(ctx, slug, tool, payload.get("args") or {})
        extra.append(
            HumanMessage(
                content=f"The user confirmed {tool} on {slug}. Result: {clip(dumps(result, 4000), 4000)}. Tell the user what happened."
            )
        )
    _t = _time.monotonic()
    prompt = system_prompt(
        user,
        conns,
        memory,
        agent=workspace_agent,
        group=group,
        teammates=teammates,
        mentions=mentions,
        skills=all_skills,
        matched_skills=matched_skills,
        query=query,
        task_state=ctx.task_state,
        task_resumed=ctx.task_resumed,
    )
    if not matched_skills and not ctx.task_state:
        # A saved skill's own steps win over the fast-path hint.
        prompt += fast_path.prompt_hint(plan)
    graph = build_agent(ctx, prompt)
    timer.mark("build", _t)
    _t = _time.monotonic()
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
            if isinstance(exc, run_cancel.TurnCancelled) or run_cancel.is_cancelled():
                raise run_cancel.TurnCancelled("stopped") from exc
            if _is_recursion_limit(exc) and attempt < 2:
                limit = min(limit + 40, 280 if coding else 200)
                if progress is not None:
                    await progress.emit("That took many steps; giving it one more pass", kind="retry")
                log.warning("recursion limit reached; retry %s/2 with limit=%s", attempt + 1, limit)
                continue
            log.exception("deep agent failed")
            text = f"I could not finish that request: {str(exc)[:240]}"
            break
    timer.mark("agent", _t)
    timer.log()
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
            "skills_used": ctx.active_skills,
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
    speaker = None
    if workspace_agent is not None:
        speaker = {
            "id": str(workspace_agent.id),
            "name": workspace_agent.name,
            "color": getattr(workspace_agent, "color", "") or "",
            "avatar": getattr(workspace_agent, "avatar", "") or "",
            "title": getattr(workspace_agent, "title", "") or "",
        }
    return {
        "text": text or "Done.",
        "pending_confirms": ctx.pending,
        "attachments": ctx.attachments,
        "used_tools": ctx.used_tools,
        "skills_used": ctx.active_skills,
        "task_state": _task_state_out(ctx),
        "route": "deep",
        "source": source,
        "mail_threads": _mail_threads(ctx),
        "browser": _browser_views(ctx),
        "computer": _computer_session(ctx),
        "connect_cards": cards,
        "speaker": speaker,
        "group_id": str(group.id) if group is not None else None,
        "workspace_changed": ctx.workspace_changed,
        "jobs_started": ctx.jobs_started,
        "question": ctx.question,
        "prs": ctx.prs,
        "merged": ctx.merged,
        "repo": ctx.repo,
        "thread": list(ctx.thread),
        "timing": timer.summary(),
    }
