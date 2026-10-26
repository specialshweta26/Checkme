from __future__ import annotations

import logging
import secrets
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.providers import get_provider
from app.auth.tokens import invalidate, seed_access_token
from app.cache import get_redis
from app.config import get_settings
from app.agent.memory import seed_letta
from app.connectors.service import ensure_builtin_connector, sync_in_background
from app.db import get_db
from app.deps import current_user, optional_user
from app.models import Account, AuthSession, Connector, User
from app.security import (
    OAUTH_COOKIE,
    SESSION_COOKIE,
    encrypt,
    issue_session_jwt,
    load_state,
    pkce_pair,
    session_expiry,
    sign_state,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


def _web(path: str) -> str:
    return f"{get_settings().public_web_url}{path}"


@router.get("/status")
async def oauth_status():
    s = get_settings()
    return {
        "google": bool(s.google_client_id),
        "microsoft": bool(s.microsoft_client_id),
        "public_web_url": s.public_web_url,
    }


@router.get("/{provider}/login")
async def login(provider: str, request: Request, mode: str = "signin", user: User | None = Depends(optional_user)):
    try:
        p = get_provider(provider)
    except KeyError:
        raise HTTPException(404, "Unknown provider")
    if not p.client_id:
        needed = "GOOGLE_CLIENT_ID" if provider == "google" else "MICROSOFT_CLIENT_ID"
        raise HTTPException(
            500,
            f"{provider} OAuth is not configured on the server. Set {needed} on the checkme-api service (not only the env group), then redeploy checkme-api.",
        )
    if mode == "link" and user is None:
        return RedirectResponse(_web("/?error=signin_required"))
    state = secrets.token_urlsafe(24)
    verifier, challenge = pkce_pair()
    cookie = sign_state(
        {"state": state, "verifier": verifier, "provider": provider, "mode": mode, "link_user": str(user.id) if user and mode == "link" else None}
    )
    resp = RedirectResponse(p.authorization_url(state, challenge))
    resp.set_cookie(
        OAUTH_COOKIE, cookie, max_age=600, httponly=True, samesite="lax", secure=get_settings().secure_cookies, path="/"
    )
    return resp


@router.get("/{provider}/callback")
async def callback(
    provider: str,
    request: Request,
    background: BackgroundTasks,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    if error:
        return RedirectResponse(_web(f"/?error={error}"))
    saved = load_state(request.cookies.get(OAUTH_COOKIE, ""))
    if not saved or not code or saved.get("state") != state or saved.get("provider") != provider:
        return RedirectResponse(_web("/?error=invalid_state"))
    p = get_provider(provider)
    try:
        tokens = await p.exchange_code(code, saved["verifier"])
        info = await p.userinfo(tokens["access_token"])
    except Exception as exc:  # noqa: BLE001
        log.warning("oauth callback failed: %s", exc)
        text = str(exc)
        if "AADSTS7000215" in text or "Invalid client secret" in text:
            return RedirectResponse(_web("/?error=bad_client_secret"))
        if "AADSTS700016" in text or "unauthorized_client" in text:
            return RedirectResponse(_web("/?error=bad_redirect_uri"))
        return RedirectResponse(_web("/?error=oauth_failed"))

    account = (
        await db.execute(select(Account).where(Account.provider == provider, Account.subject == info["subject"]))
    ).scalar_one_or_none()

    if saved.get("mode") == "link":
        user = await db.get(User, uuid.UUID(saved["link_user"])) if saved.get("link_user") else None
        if user is None:
            return RedirectResponse(_web("/?error=signin_required"))
        if account and account.user_id != user.id:
            return RedirectResponse(_web("/connectors?error=account_linked_elsewhere"))
    else:
        user = await db.get(User, account.user_id) if account else None
        if user is None:
            # No auto-merge by email: a matching email on another provider must be linked explicitly.
            user = User(
                email=info["email"],
                display_name=info["name"],
                primary_provider=provider,
                timezone="Asia/Kolkata",
            )
            db.add(user)
            await db.flush()

    if account is None:
        account = Account(user_id=user.id, provider=provider, subject=info["subject"], email=info["email"])
        db.add(account)
    account.email = info["email"]
    account.scopes = tokens.get("scope", " ".join(p.scopes))
    if tokens.get("refresh_token"):
        account.refresh_token_enc = encrypt(tokens["refresh_token"])
    await db.flush()

    connector = await ensure_builtin_connector(db, user, account)
    from app.connectors.platform import ensure_platform_connectors

    await ensure_platform_connectors(db, user)
    sess = AuthSession(user_id=user.id, expires_at=session_expiry(), user_agent=request.headers.get("user-agent", "")[:300])
    db.add(sess)
    await db.commit()

    await invalidate(account_id=account.id)
    await seed_access_token(account.id, tokens["access_token"], int(tokens.get("expires_in", 3600)))
    background.add_task(sync_in_background, connector.id, True)
    if not user.letta_agent_id:
        background.add_task(seed_letta, user.id)

    target = "/connectors?linked=1" if saved.get("mode") == "link" else "/chat"
    resp = RedirectResponse(_web(target))
    resp.delete_cookie(OAUTH_COOKIE, path="/")
    resp.set_cookie(
        SESSION_COOKIE,
        issue_session_jwt(user.id, sess.id, sess.expires_at),
        max_age=get_settings().session_ttl_days * 86400,
        httponly=True,
        samesite="lax",
        secure=get_settings().secure_cookies,
        path="/",
    )
    return resp


@router.post("/logout")
async def logout(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    sid = getattr(request.state, "auth_session_id", None)
    if sid:
        sess = await db.get(AuthSession, sid)
        if sess:
            sess.revoked = True
            await db.commit()
        await get_redis().set(f"revoked:{sid}", b"1", ex=get_settings().session_ttl_days * 86400)
    resp = RedirectResponse(_web("/"), status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


me_router = APIRouter(tags=["me"])


@me_router.get("/me")
async def me(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    conns = (await db.execute(select(Connector).where(Connector.user_id == user.id))).scalars().all()
    accounts = (await db.execute(select(Account).where(Account.user_id == user.id))).scalars().all()
    return {
        "user_id": str(user.id),
        "email": user.email,
        "name": user.display_name,
        "primary_provider": user.primary_provider,
        "timezone": user.timezone,
        "accounts": [{"provider": a.provider, "email": a.email} for a in accounts],
        "connectors": [
            {"id": str(c.id), "name": c.alias or c.name, "slug": c.slug, "provider": c.provider, "health": c.health, "enabled": c.enabled}
            for c in conns
        ],
    }


@me_router.patch("/me")
async def update_me(body: dict, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    from zoneinfo import ZoneInfo

    tz = body.get("timezone")
    if tz:
        try:
            ZoneInfo(tz)
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "Unknown timezone")
        user.timezone = tz
    await db.commit()
    return {"ok": True}
