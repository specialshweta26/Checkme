from __future__ import annotations

import asyncio
from typing import Any

import httpx

from connector_core.spec import ConnectorAuthError, ToolError, TokenProvider

_shared: httpx.AsyncClient | None = None


def shared_client() -> httpx.AsyncClient:
    """One pooled client per process; keeps TLS connections to Google/Graph warm."""
    global _shared
    if _shared is None or _shared.is_closed:
        _shared = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=5.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
            http2=False,
        )
    return _shared


class ApiClient:
    def __init__(self, label: str, token_provider: TokenProvider, http: httpx.AsyncClient | None = None):
        self._label = label
        self._token = token_provider
        self._http = http or shared_client()

    async def raw(self, method: str, url: str, *, headers: dict | None = None, **kw: Any) -> httpx.Response:
        for attempt in range(3):
            token = await self._token()
            hdrs = {"Authorization": f"Bearer {token}", **(headers or {})}
            resp = await self._http.request(method, url, headers=hdrs, **kw)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 0.5 * (2**attempt)
                await asyncio.sleep(min(delay, 5.0))
                continue
            break
        if resp.status_code == 401:
            raise ConnectorAuthError(f"{self._label} rejected the credentials; reconnect the account")
        if resp.status_code == 403:
            raise ToolError(f"{self._label} denied access (missing permission/scope): {resp.text[:200]}")
        if resp.status_code == 404:
            raise ToolError(f"{self._label}: item not found")
        if resp.status_code >= 400:
            raise ToolError(f"{self._label} error {resp.status_code}: {resp.text[:300]}")
        return resp

    async def json(self, method: str, url: str, **kw: Any) -> dict:
        resp = await self.raw(method, url, **kw)
        if not resp.content:
            return {}
        return resp.json()


async def gather_limited(coros: list, limit: int = 10) -> list:
    sem = asyncio.Semaphore(limit)

    async def run(c):
        async with sem:
            return await c

    return await asyncio.gather(*(run(c) for c in coros))
