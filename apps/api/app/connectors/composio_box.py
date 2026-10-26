"""Composio marketplace — search apps, connect accounts, execute tools."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

import httpx

from connector_core import ToolError, ToolSpec
from connector_core.http import shared_client
from app.config import get_settings

_TOOLKIT_CACHE: tuple[float, str, list] | None = None
_TOOLKIT_TTL = 120.0

COMPOSIO_API = "https://backend.composio.dev/api/v3.1"

COMPOSIO_TOOLS = [
    ToolSpec(
        name="find_tools",
        description="Search the Composio marketplace for tools that match a capability. Returns names and descriptions, not data.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Capability phrase, e.g. create github issue."},
                "toolkit": {"type": "string", "description": "Optional toolkit slug such as github or slack."},
            },
            "required": ["query"],
        },
        group="catalog",
        tags=("marketplace", "catalog"),
    ),
    ToolSpec(
        name="execute",
        description="Run a Composio marketplace tool. arguments is the inner tool's JSON payload.",
        input_schema={
            "type": "object",
            "properties": {
                "tool_name": {"type": "string", "description": "Exact Composio tool slug from find_tools."},
                "arguments": {"type": "object", "description": "Arguments for that tool."},
            },
            "required": ["tool_name"],
        },
        group="write",
        read_only=False,
        tags=("marketplace",),
    ),
    ToolSpec(
        name="resolve",
        description="Return the JSON schema for one Composio tool slug.",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Composio tool slug."}},
            "required": ["name"],
        },
        group="catalog",
        tags=("marketplace", "catalog"),
    ),
    ToolSpec(
        name="list_apps",
        description="Browse Composio marketplace apps/toolkits. Catalog only — not an answer.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Optional app name filter."}},
        },
        group="catalog",
        tags=("marketplace", "catalog"),
    ),
    ToolSpec(
        name="connect_app",
        description=(
            "Start OAuth for one Composio app. toolkit is the app slug the user named (gmail, github, slack). "
            "Returns redirect_url; the chat shows it as a connect card. Do not execute another vendor's tools."
        ),
        input_schema={
            "type": "object",
            "properties": {"toolkit": {"type": "string", "description": "Toolkit slug, e.g. github."}},
            "required": ["toolkit"],
        },
        group="write",
        read_only=False,
        tags=("marketplace",),
    ),
    ToolSpec(
        name="list_connections",
        description="List this user's connected Composio apps.",
        input_schema={"type": "object", "properties": {}},
        group="catalog",
        tags=("marketplace", "catalog"),
    ),
]


def _key() -> str:
    key = (get_settings().composio_api_key or "").strip()
    if not key:
        raise ToolError("COMPOSIO_API_KEY is not configured.")
    return key


def _headers() -> dict[str, str]:
    return {"x-api-key": _key(), "Content-Type": "application/json"}


def _user_id(row) -> str:
    return str(row.user_id)


def _error_text(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(data, dict) and data.get("message"):
            return str(data["message"])
    except Exception:  # noqa: BLE001
        pass
    return (resp.text or resp.reason_phrase or "Composio error")[:400]


async def composio_request(method: str, path: str, **kwargs) -> Any:
    url = path if path.startswith("http") else f"{COMPOSIO_API}{path}"
    timeout = kwargs.pop("timeout", httpx.Timeout(12.0, connect=3.0))
    resp = await shared_client().request(method, url, headers=_headers(), timeout=timeout, **kwargs)
    if resp.status_code >= 400:
        raise ToolError(_error_text(resp))
    if not resp.content:
        return {}
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {"text": resp.text[:4000]}


def _items(data: Any) -> list[dict]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("items", "toolkits", "data", "connected_accounts"):
            val = data.get(key)
            if isinstance(val, list):
                return [item for item in val if isinstance(item, dict)]
    return []


def slim_tool(item: dict) -> dict:
    toolkit = item.get("toolkit") if isinstance(item.get("toolkit"), dict) else {}
    return {
        "public_name": item.get("slug") or item.get("name"),
        "name": item.get("slug") or item.get("name"),
        "description": (item.get("description") or item.get("name") or "")[:800],
        "toolkit": toolkit.get("slug") or item.get("toolkit_slug"),
        "score": item.get("score") or 0.6,
        "no_auth": item.get("no_auth"),
    }


async def list_toolkits(query: str = "", limit: int = 40) -> list[dict]:
    global _TOOLKIT_CACHE
    cache_key = f"{query}|{limit}"
    if _TOOLKIT_CACHE and _TOOLKIT_CACHE[1] == cache_key and time.monotonic() - _TOOLKIT_CACHE[0] < _TOOLKIT_TTL:
        return list(_TOOLKIT_CACHE[2])
    params: dict[str, Any] = {"limit": min(limit, 100)}
    if query:
        params["search"] = query
    data = await composio_request("GET", f"/toolkits?{urlencode(params)}")
    out = []
    for item in _items(data):
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        out.append(
            {
                "slug": item.get("slug"),
                "name": item.get("name") or item.get("slug"),
                "description": (meta.get("description") or item.get("description") or "")[:240],
                "logo": meta.get("logo"),
                "auth_schemes": item.get("auth_schemes") or [],
                "no_auth": item.get("no_auth"),
                "tools_count": meta.get("tools_count"),
                "categories": [c.get("name") for c in (meta.get("categories") or []) if isinstance(c, dict)],
            }
        )
    if query and not out:
        q = query.lower()
        data = await composio_request("GET", "/toolkits?limit=80")
        for item in _items(data):
            blob = f"{item.get('slug')} {item.get('name')}".lower()
            if q in blob:
                meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
                out.append(
                    {
                        "slug": item.get("slug"),
                        "name": item.get("name") or item.get("slug"),
                        "description": (meta.get("description") or "")[:240],
                        "logo": meta.get("logo"),
                        "auth_schemes": item.get("auth_schemes") or [],
                        "no_auth": item.get("no_auth"),
                        "tools_count": meta.get("tools_count"),
                        "categories": [c.get("name") for c in (meta.get("categories") or []) if isinstance(c, dict)],
                    }
                )
    _TOOLKIT_CACHE = (time.monotonic(), cache_key, out)
    return out


async def search_tools(query: str, toolkit: str | None = None, limit: int = 12) -> list[dict]:
    params: dict[str, Any] = {"query": query, "limit": limit}
    if toolkit:
        params["toolkit_slug"] = toolkit
    data = await composio_request("GET", f"/tools?{urlencode(params)}")
    return [slim_tool(item) for item in _items(data)]


async def get_tool(slug: str) -> dict:
    data = await composio_request("GET", f"/tools/{slug}")
    item = data.get("item") if isinstance(data.get("item"), dict) else data
    schema = item.get("input_parameters") or item.get("input_schema") or {"type": "object", "properties": {}}
    return {
        "public_name": item.get("slug") or slug,
        "name": item.get("slug") or slug,
        "description": item.get("description") or "",
        "input_schema": schema,
        "parameters": schema,
        "toolkit": (item.get("toolkit") or {}).get("slug") if isinstance(item.get("toolkit"), dict) else None,
    }


async def execute_tool(user_id: str, slug: str, arguments: dict | None = None) -> Any:
    return await composio_request(
        "POST",
        f"/tools/execute/{slug}",
        json={"user_id": user_id, "arguments": arguments or {}, "version": "latest"},
        timeout=httpx.Timeout(45.0, connect=5.0),
    )


async def list_connections(user_id: str) -> list[dict]:
    data = await composio_request("GET", f"/connected_accounts?{urlencode({'user_ids': user_id, 'limit': 50})}")
    out = []
    for item in _items(data):
        toolkit = item.get("toolkit") if isinstance(item.get("toolkit"), dict) else {}
        out.append(
            {
                "id": item.get("id") or item.get("nano_id"),
                "status": item.get("status"),
                "toolkit": toolkit.get("slug") or item.get("toolkit_slug"),
                "created_at": item.get("created_at"),
            }
        )
    return out


async def ensure_auth_config(toolkit: str) -> str:
    data = await composio_request("GET", f"/auth_configs?{urlencode({'toolkit_slug': toolkit, 'limit': 10})}")
    for item in _items(data):
        if str(item.get("status") or "").upper() in {"", "ENABLED", "ACTIVE"}:
            if item.get("id"):
                return str(item["id"])
    payloads = [
        {"toolkit": {"slug": toolkit}, "auth_scheme": "OAUTH2"},
        {"toolkit": {"slug": toolkit}, "use_composio_managed_auth": True},
        {"toolkit": {"slug": toolkit}},
    ]
    last_error = "could not create auth config"
    for body in payloads:
        try:
            created = await composio_request("POST", "/auth_configs", json=body)
            cid = created.get("id") or (created.get("auth_config") or {}).get("id")
            if cid:
                return str(cid)
        except ToolError as exc:
            last_error = str(exc)
    raise ToolError(last_error)


async def start_link(user_id: str, toolkit: str, callback_url: str) -> dict:
    auth_config_id = await ensure_auth_config(toolkit)
    data = await composio_request(
        "POST",
        "/connected_accounts/link",
        json={"auth_config_id": auth_config_id, "user_id": user_id, "callback_url": callback_url},
    )
    return {
        "toolkit": toolkit,
        "redirect_url": data.get("redirect_url") or data.get("redirectUrl"),
        "connected_account_id": data.get("connected_account_id"),
        "expires_at": data.get("expires_at"),
    }


async def call_composio(row, name: str, args: dict[str, Any]) -> Any:
    user_id = _user_id(row)
    if name == "find_tools":
        hits = await search_tools(args.get("query") or "", args.get("toolkit"), 12)
        return {"query": args.get("query") or "", "candidates": hits}
    if name == "execute":
        slug = args.get("tool_name") or args.get("tool_slug") or args.get("name")
        if not slug:
            raise ToolError("tool_name is required")
        inner = args.get("arguments") if isinstance(args.get("arguments"), dict) else {}
        try:
            return await execute_tool(user_id, str(slug), inner)
        except ToolError as exc:
            return {
                "error": str(exc),
                "hint": "If this app is not connected, call connect_app with the toolkit slug the user named, not a different vendor.",
            }
    if name == "resolve":
        slug = args.get("name") or args.get("tool_name") or args.get("public_name")
        if not slug:
            raise ToolError("name is required")
        return await get_tool(str(slug))
    if name == "list_apps":
        return {"apps": await list_toolkits(args.get("query") or "", 40)}
    if name == "connect_app":
        toolkit = str(args.get("toolkit") or args.get("slug") or "").strip().lower()
        if not toolkit:
            raise ToolError("toolkit is required")
        callback = f"{get_settings().public_web_url.rstrip('/')}/connectors?composio=connected"
        data = await start_link(user_id, toolkit, callback)
        meta: dict = {}
        try:
            apps = await list_toolkits(toolkit, 8)
            meta = next((row for row in apps if str(row.get("slug") or "").lower() == toolkit), {}) or {}
            if not meta and apps:
                meta = apps[0]
        except Exception:  # noqa: BLE001
            meta = {}
        return {
            **data,
            "toolkit": toolkit,
            "name": meta.get("name") or toolkit,
            "logo": meta.get("logo"),
            "description": (meta.get("description") or "")[:240],
        }
    if name == "list_connections":
        return {"connections": await list_connections(user_id)}
    raise ToolError(f"Unknown composio tool '{name}'")
