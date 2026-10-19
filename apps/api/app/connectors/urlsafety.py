from __future__ import annotations

import asyncio
import ipaddress
from urllib.parse import urlparse

from app.config import get_settings


class UnsafeUrl(ValueError):
    pass


async def validate_public_url(url: str) -> str:
    """Block SSRF: user-supplied MCP and OAuth URLs must resolve to public addresses."""
    parsed = urlparse(url)
    allow_private = get_settings().allow_private_mcp_urls
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise UnsafeUrl("URL must be http(s) with a host")
    if parsed.scheme == "http" and not allow_private:
        raise UnsafeUrl("Only https URLs are allowed")
    if allow_private:
        return url
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, parsed.port or 443)
    except OSError as exc:
        raise UnsafeUrl(f"Cannot resolve host: {exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise UnsafeUrl("URL resolves to a private or reserved address")
    return url
