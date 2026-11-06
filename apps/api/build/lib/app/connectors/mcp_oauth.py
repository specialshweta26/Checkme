from __future__ import annotations

from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

from connector_core.http import shared_client

from app.config import get_settings
from app.connectors.urlsafety import validate_public_url


async def _get_json(url: str) -> dict:
    await validate_public_url(url)
    resp = await shared_client().get(url, headers={"Accept": "application/json"}, follow_redirects=True)
    if resp.status_code >= 400:
        return {}
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {}


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


async def discover_auth(server_url: str) -> dict[str, Any]:
    """MCP authorization discovery. Best-effort; empty dict means no OAuth metadata."""
    await validate_public_url(server_url)
    origin = _origin(server_url)
    resource = await _get_json(urljoin(origin + "/", ".well-known/oauth-protected-resource"))
    as_url = None
    servers = resource.get("authorization_servers") or resource.get("authorization_server")
    if isinstance(servers, list) and servers:
        as_url = servers[0]
    elif isinstance(servers, str):
        as_url = servers
    if not as_url:
        as_url = origin
    as_origin = _origin(as_url)
    meta = await _get_json(urljoin(as_origin + "/", ".well-known/oauth-authorization-server"))
    if not meta:
        meta = await _get_json(urljoin(as_origin + "/", ".well-known/openid-configuration"))
    if not meta:
        return {}
    return {
        "resource": resource.get("resource") or server_url,
        "authorization_endpoint": meta.get("authorization_endpoint"),
        "token_endpoint": meta.get("token_endpoint"),
        "registration_endpoint": meta.get("registration_endpoint"),
        "scopes": meta.get("scopes_supported") or [],
    }


async def register_client(registration_endpoint: str) -> dict:
    await validate_public_url(registration_endpoint)
    redirect = redirect_uri()
    body = {
        "client_name": "Lumio",
        "redirect_uris": [redirect],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "application_type": "web",
    }
    resp = await shared_client().post(registration_endpoint, json=body, timeout=15)
    if resp.status_code >= 400:
        raise PermissionError(f"dynamic client registration failed: {resp.text[:300]}")
    return resp.json()


def redirect_uri() -> str:
    return f"{get_settings().public_web_url.rstrip('/')}/api/connectors/oauth/callback"


def authorize_url(meta: dict, client_id: str, state: str, challenge: str, scope: str = "") -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    scopes = scope or " ".join(meta.get("scopes") or []) or "mcp:connect"
    if scopes:
        params["scope"] = scopes
    if meta.get("resource"):
        params["resource"] = meta["resource"]
    return f"{meta['authorization_endpoint']}?{urlencode(params)}"


async def exchange_mcp_code(meta: dict, client_id: str, client_secret: str | None, code: str, verifier: str) -> dict:
    await validate_public_url(meta["token_endpoint"])
    data = {
        "client_id": client_id,
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri(),
        "grant_type": "authorization_code",
    }
    if client_secret:
        data["client_secret"] = client_secret
    if meta.get("resource"):
        data["resource"] = meta["resource"]
    resp = await shared_client().post(meta["token_endpoint"], data=data)
    if resp.status_code >= 400:
        raise PermissionError(f"MCP token exchange failed: {resp.text[:300]}")
    return resp.json()


async def refresh_mcp_token(creds: dict) -> dict:
    token_url = creds.get("token_endpoint")
    if not token_url:
        raise PermissionError("no token endpoint stored")
    await validate_public_url(token_url)
    data = {
        "client_id": creds.get("client_id", ""),
        "refresh_token": creds["refresh_token"],
        "grant_type": "refresh_token",
    }
    if creds.get("client_secret"):
        data["client_secret"] = creds["client_secret"]
    if creds.get("resource"):
        data["resource"] = creds["resource"]
    resp = await shared_client().post(token_url, data=data)
    if resp.status_code >= 400:
        raise PermissionError(f"MCP refresh failed: {resp.text[:300]}")
    return resp.json()


def pack_oauth_creds(meta: dict, tokens: dict, client_id: str, client_secret: str | None) -> dict:
    import time

    return {
        "token_endpoint": meta.get("token_endpoint"),
        "resource": meta.get("resource"),
        "client_id": client_id,
        "client_secret": client_secret,
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "expires_at": time.time() + int(tokens.get("expires_in", 3600)),
    }


def pack_bearer(token: str, header: str = "Authorization", prefix: str = "Bearer ") -> dict:
    return {"access_token": token, "header": header, "prefix": prefix}
