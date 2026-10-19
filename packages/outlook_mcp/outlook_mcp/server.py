"""Standalone Outlook (Microsoft Graph) MCP server over stdio.

Env: MICROSOFT_CLIENT_ID, MICROSOFT_CLIENT_SECRET, MICROSOFT_REFRESH_TOKEN, MICROSOFT_TENANT (default common),
or MICROSOFT_ACCESS_TOKEN for a quick test.
"""

from __future__ import annotations

import asyncio
import os
import time

from connector_core.http import shared_client
from connector_core.mcp_server import serve_stdio
from outlook_mcp.connector import OutlookConnector

SCOPES = "offline_access User.Read Mail.ReadWrite Mail.Send Calendars.Read"
_cache: dict[str, float | str] = {}


async def _token() -> str:
    if os.getenv("MICROSOFT_ACCESS_TOKEN"):
        return os.environ["MICROSOFT_ACCESS_TOKEN"]
    if _cache.get("exp", 0) > time.time() + 60:
        return str(_cache["token"])
    tenant = os.getenv("MICROSOFT_TENANT", "common")
    resp = await shared_client().post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data={
            "client_id": os.environ["MICROSOFT_CLIENT_ID"],
            "client_secret": os.environ["MICROSOFT_CLIENT_SECRET"],
            "refresh_token": str(_cache.get("refresh") or os.environ["MICROSOFT_REFRESH_TOKEN"]),
            "grant_type": "refresh_token",
            "scope": SCOPES,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    _cache.update(
        token=data["access_token"],
        exp=time.time() + int(data.get("expires_in", 3600)),
        refresh=data.get("refresh_token") or _cache.get("refresh", ""),
    )
    return data["access_token"]


def main() -> None:
    asyncio.run(serve_stdio(OutlookConnector(_token), "outlook"))


if __name__ == "__main__":
    main()
