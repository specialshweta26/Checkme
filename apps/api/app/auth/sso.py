"""Enterprise single sign-on over OpenID Connect (docs/AUTH.md "SSO").

Works with any OIDC identity provider: Okta, Microsoft Entra ID, Google Workspace, Auth0, Keycloak, OneLogin,
JumpCloud, Ping. Connections are set in SSO_CONNECTIONS (JSON); each names the email domains it signs in. The user
enters their work email, Lumio finds the connection by domain and sends them to their company's sign-in page.

The callback checks everything itself: state and PKCE, then the ID token's signature (the provider's published keys),
issuer, audience, expiry and nonce, and that the email is verified and on one of the connection's domains. A new
person is created on first sign-in; after that the provider's stable subject id, not the email, finds them.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from urllib.parse import urlencode

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth/sso", tags=["auth"])

SSO_COOKIE = "lumio_sso"
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,15}$")
_DISCOVERY_TTL_S = 3600
_cache: dict[str, tuple[float, dict]] = {}


def connections() -> list[dict]:
    """The valid connections from SSO_CONNECTIONS; a broken entry is skipped (and logged), never fatal."""
    raw = (get_settings().sso_connections or "").strip()
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except ValueError:
        log.warning("SSO_CONNECTIONS is not valid JSON")
        return []
    out = []
    for c in items if isinstance(items, list) else []:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "").strip().lower()
        issuer = str(c.get("issuer") or "").strip().rstrip("/")
        domains = [str(d).strip().lower().lstrip("@") for d in (c.get("domains") or []) if str(d).strip()]
        if not (_ID.fullmatch(cid) and issuer.startswith("https://") and c.get("client_id") and domains):
            log.warning("SSO connection %r skipped: needs id, https issuer, client_id and domains", cid or "?")
            continue
        out.append({**c, "id": cid, "issuer": issuer, "domains": domains, "name": str(c.get("name") or cid)})
    return out


def for_email(email: str) -> dict | None:
    domain = (email or "").rsplit("@", 1)[-1].strip().lower()
    if not domain:
        return None
    return next((c for c in connections() if domain in c["domains"]), None)


def by_id(cid: str) -> dict:
    conn = next((c for c in connections() if c["id"] == (cid or "").lower()), None)
    if conn is None:
        raise HTTPException(404, "Unknown SSO connection.")
    return conn


def redirect_uri(conn: dict) -> str:
    return f"{get_settings().public_web_url}/api/auth/sso/{conn['id']}/callback"


async def _get_json(url: str) -> dict:
    from connector_core.http import shared_client

    resp = await shared_client().get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()


async def discovery(conn: dict) -> dict:
    url = conn.get("discovery_url") or f"{conn['issuer']}/.well-known/openid-configuration"
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < _DISCOVERY_TTL_S:
        return hit[1]
    doc = await _get_json(url)
    _cache[url] = (time.time(), doc)
    return doc


async def _signing_key(doc: dict, kid: str | None):
    jwks = await _get_json(doc["jwks_uri"])
    keys = jwks.get("keys") or []
    for k in keys:
        if kid is None or k.get("kid") == kid:
            return jwt.PyJWK(k).key
    raise ValueError("no signing key for the ID token")


def _issuer_ok(conn: dict, doc: dict, iss: str) -> bool:
    expected = {conn["issuer"], str(doc.get("issuer") or "").rstrip("/")}
    if (iss or "").rstrip("/") in expected:
        return True
    # Entra ID's multi-tenant metadata says ".../{tenantid}/v2.0"; the token carries the real tenant id.
    template = str(doc.get("issuer") or "")
    if "{tenantid}" in template:
        pattern = re.escape(template).replace(re.escape("{tenantid}"), r"[0-9a-f-]{36}")
        return re.fullmatch(pattern, iss or "") is not None
    return False


async def verify_id_token(conn: dict, doc: dict, id_token: str, nonce: str) -> dict:
    header = jwt.get_unverified_header(id_token)
    alg = header.get("alg") or "RS256"
    if alg not in ("RS256", "RS384", "RS512", "ES256", "ES384", "PS256"):
        raise ValueError(f"unsupported ID token algorithm {alg}")
    key = await _signing_key(doc, header.get("kid"))
    claims = jwt.decode(id_token, key=key, algorithms=[alg], audience=conn["client_id"], options={"verify_iss": False},
                        leeway=60)  # fmt: skip
    if not _issuer_ok(conn, doc, str(claims.get("iss") or "")):
        raise ValueError("ID token issuer doesn't match the connection")
    if claims.get("nonce") != nonce:
        raise ValueError("ID token nonce mismatch")
    return claims


@router.get("/discover")
async def discover(email: str = ""):
    """Which SSO connection signs in this email's domain (``{"sso": false}`` when none)."""
    conn = for_email(email)
    if conn is None:
        return {"sso": False}
    return {"sso": True, "id": conn["id"], "name": conn["name"], "enforce": bool(conn.get("enforce"))}


@router.get("/{cid}/login")
async def sso_login(cid: str, email: str = "", desktop: str = ""):
    from app.auth.routes import DESKTOP_CODE, _web
    from app.security import pkce_pair, sign_state

    conn = by_id(cid)
    if desktop and not DESKTOP_CODE.fullmatch(desktop):
        return RedirectResponse(_web("/login?error=invalid_state"))
    try:
        doc = await discovery(conn)
    except Exception as exc:  # noqa: BLE001
        log.warning("SSO discovery for %s failed: %s", conn["id"], type(exc).__name__)
        return RedirectResponse(_web("/login?error=sso_unreachable"))
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    verifier, challenge = pkce_pair()
    params = {
        "client_id": conn["client_id"], "redirect_uri": redirect_uri(conn), "response_type": "code",
        "scope": conn.get("scopes") or "openid email profile", "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256",
    }  # fmt: skip
    if email and "@" in email:
        params["login_hint"] = email.strip()
    resp = RedirectResponse(f"{doc['authorization_endpoint']}?{urlencode(params)}")
    cookie = sign_state({"state": state, "nonce": nonce, "verifier": verifier, "sso": conn["id"], "desktop": desktop or None})
    s = get_settings()
    resp.set_cookie(SSO_COOKIE, cookie, max_age=600, httponly=True, samesite="lax", secure=s.secure_cookies, path="/")
    return resp


@router.get("/{cid}/callback")
async def sso_callback(cid: str, request: Request, code: str = "", state: str = "", error: str = "",
                       db: AsyncSession = Depends(get_db)):  # fmt: skip
    from connector_core.http import shared_client

    from app.auth.routes import DESKTOP_CODE, _session_cookie, _web, desktop_handoff, new_session
    from app.models import Account, User
    from app.security import load_state

    if error:
        return RedirectResponse(_web(f"/login?error=sso_{re.sub(r'[^a-z_]', '', error.lower())[:40]}"))
    conn = by_id(cid)
    saved = load_state(request.cookies.get(SSO_COOKIE, ""))
    if not saved or not code or saved.get("state") != state or saved.get("sso") != conn["id"]:
        return RedirectResponse(_web("/login?error=invalid_state"))
    try:
        doc = await discovery(conn)
        resp = await shared_client().post(doc["token_endpoint"], data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(conn),
            "client_id": conn["client_id"], "client_secret": conn.get("client_secret") or "", "code_verifier": saved["verifier"],
        }, timeout=20)  # fmt: skip
        if resp.status_code >= 400:
            raise ValueError(f"token exchange {resp.status_code}: {resp.text[:200]}")
        claims = await verify_id_token(conn, doc, resp.json().get("id_token") or "", saved["nonce"])
    except Exception as exc:  # noqa: BLE001
        log.warning("SSO sign-in via %s failed: %s", conn["id"], exc)
        return RedirectResponse(_web("/login?error=sso_failed"))
    email = str(claims.get("email") or claims.get("preferred_username") or claims.get("upn") or "").strip().lower()
    subject = str(claims.get("sub") or "")
    if not subject or "@" not in email or email.rsplit("@", 1)[1] not in conn["domains"]:
        return RedirectResponse(_web("/login?error=sso_wrong_domain"))
    if claims.get("email_verified") is False:
        return RedirectResponse(_web("/login?error=sso_unverified_email"))
    provider = f"sso:{conn['id']}"
    account = (await db.execute(select(Account).where(Account.provider == provider, Account.subject == subject))).scalar_one_or_none()
    user = await db.get(User, account.user_id) if account else None
    if user is None:
        name = str(claims.get("name") or " ".join(x for x in (claims.get("given_name"), claims.get("family_name")) if x) or email.split("@")[0])
        user = User(email=email, display_name=name[:200], primary_provider="sso", timezone="Asia/Kolkata")
        db.add(user)
        await db.flush()
        account = Account(user_id=user.id, provider=provider, subject=subject, email=email, scopes="openid email profile")
        db.add(account)
    account.email = email
    await db.flush()
    if user.suspended_at is not None:
        await db.rollback()
        return RedirectResponse(_web("/login?error=suspended"))
    token = await new_session(db, request, user, how=provider)
    target = "/chat"
    desk = saved.get("desktop") or ""
    if desk and DESKTOP_CODE.fullmatch(desk):
        target = await desktop_handoff(db, user, desk)
    out = RedirectResponse(_web(target))
    out.delete_cookie(SSO_COOKIE, path="/")
    _session_cookie(out, token)
    return out
