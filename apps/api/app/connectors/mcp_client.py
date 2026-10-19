from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from app.connectors.urlsafety import validate_public_url
from app.security import decrypt_json


def _headers(creds: dict) -> dict[str, str]:
    token = creds.get("access_token")
    if not token:
        return {}
    header = creds.get("header") or "Authorization"
    prefix = creds.get("prefix", "Bearer ")
    return {header: f"{prefix}{token}"}


@asynccontextmanager
async def mcp_session(server_url: str, creds: dict):
    await validate_public_url(server_url)
    headers = _headers(creds)
    async with create_mcp_http_client(headers=headers or None) as http:
        async with streamable_http_client(server_url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def list_mcp_tools(server_url: str, creds: dict) -> list:
    async with mcp_session(server_url, creds) as session:
        result = await session.list_tools()
        return list(result.tools)


async def call_mcp_tool(server_url: str, creds: dict, name: str, args: dict[str, Any]) -> Any:
    async with mcp_session(server_url, creds) as session:
        result = await session.call_tool(name, args or {})
        texts = []
        for block in result.content or []:
            kind = getattr(block, "type", "")
            if kind == "text":
                texts.append(block.text)
            elif kind == "resource":
                res = getattr(block, "resource", None)
                texts.append(json.dumps({"uri": getattr(res, "uri", ""), "mime": getattr(res, "mimeType", "")}))
        joined = "\n".join(texts) if texts else ""
        if getattr(result, "isError", False):
            raise RuntimeError(joined or "MCP tool error")
        try:
            return json.loads(joined) if joined else {"ok": True}
        except json.JSONDecodeError:
            return {"text": joined[:8000]}


def hostname_slug(url: str) -> str:
    host = urlparse(url).hostname or "mcp"
    return host.replace(".", "-")[:32]
