"""Standalone Gmail MCP server over stdio.

Env: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN (or GOOGLE_ACCESS_TOKEN for a quick test).
"""

from __future__ import annotations

import asyncio
import os
import time

from connector_core.http import shared_client
from connector_core.mcp_server import serve_stdio
from gmail_mcp.connector import GmailConnector

_cache: dict[str, float | str] = {}


async def _token() -> str:
    if os.getenv("GOOGLE_ACCESS_TOKEN"):
        return os.environ["GOOGLE_ACCESS_TOKEN"]
    if _cache.get("exp", 0) > time.time() + 60:
        return str(_cache["token"])
    resp = await shared_client().post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "refresh_token": os.environ["GOOGLE_REFRESH_TOKEN"],
            "grant_type": "refresh_token",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    _cache.update(token=data["access_token"], exp=time.time() + int(data.get("expires_in", 3600)))
    return data["access_token"]


def main() -> None:
    asyncio.run(serve_stdio(GmailConnector(_token), "gmail"))


if __name__ == "__main__":
    main()
