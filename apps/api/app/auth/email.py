"""Email and password sign-in: sign up, verify the address, sign in, forgot and reset password (docs/AUTH.md).

- Sign-up never says whether an address already has an account: the answer is always "check your email", and the
  mail tells an existing user to sign in or reset instead.
- Only a verified address can sign in. Re-signing up an unverified address replaces its password, so someone who
  registered another person's address first gains nothing.
- Verify and reset links are signed, expire (a day, an hour), and die once the password changes. A reset signs out
  every other session.
- Sign-in attempts are limited per address and per IP, and an address locks for 15 minutes after 8 failures.
- A domain whose SSO connection is set to ``enforce`` can't use a password.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import access, mailer, passwords, sso
from app.config import get_settings
from app.db import get_db
from app.deps import current_user
from app.models import Account, AuthSession, PasswordLogin, User

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth/email", tags=["auth"])

VERIFY_TTL_S = 24 * 3600
RESET_TTL_S = 3600
MAX_FAILURES = 8
LOCK_MINUTES = 15
_EMAIL = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}$")
CHECK_EMAIL = {"ok": True, "message": "Check your email: if this address can be used, we've sent you a link."}


def normalize_email(email: str) -> str:
    value = (email or "").strip().lower()
    if not _EMAIL.fullmatch(value):
        raise HTTPException(400, "Enter a valid email address.")
    return value


def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().session_secret, salt=salt)


def make_token(kind: str, row: PasswordLogin) -> str:
    return _serializer(f"lumio-{kind}").dumps({"e": row.email, "f": passwords.fingerprint(row.password_hash)})


def read_token(kind: str, token: str, max_age: int) -> dict:
    try:
        data = _serializer(f"lumio-{kind}").loads(token or "", max_age=max_age)
    except SignatureExpired:
        raise HTTPException(400, "This link has expired. Ask for a new one.") from None
    except BadSignature:
        raise HTTPException(400, "This link isn't valid. Ask for a new one.") from None
    return data if isinstance(data, dict) else {}


def _link(path: str, token: str) -> str:
    return f"{get_settings().public_web_url}{path}?token={token}"


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else "") or (request.client.host if request.client else "")


async def _limited(key: str, limit: int, window_s: int) -> bool:
    """True when ``key`` went over ``limit`` hits in ``window_s``. A Redis outage doesn't block (the per-address
    lockout in the database still holds)."""
    try:
        from app.cache import get_redis

        r = get_redis()
        n = await r.incr(f"rl:{key}")
        if n == 1:
            await r.expire(f"rl:{key}", window_s)
        return n > limit
    except Exception:  # noqa: BLE001
        return False


def _enforced_sso(email: str) -> None:
    conn = sso.for_email(email)
    if conn and conn.get("enforce"):
        raise HTTPException(409, f"{conn.get('name') or 'Your organization'} uses single sign-on. Continue with SSO instead.")


def _available() -> None:
    if not get_settings().email_login or not mailer.configured():
        raise HTTPException(404, "Email sign-in isn't set up on this server.")


async def _login_row(db: AsyncSession, email: str) -> PasswordLogin | None:
    return (await db.execute(select(PasswordLogin).where(PasswordLogin.email == email))).scalar_one_or_none()


class SignupIn(BaseModel):
    email: str
    password: str
    name: str = ""


@router.post("/signup")
async def signup(body: SignupIn, request: Request, db: AsyncSession = Depends(get_db)):
    _available()
    email = normalize_email(body.email)
    _enforced_sso(email)
    if not access.may_sign_up(email):
        raise HTTPException(403, access.CLOSED_MESSAGE)
    why = passwords.problem(body.password, email)
    if why:
        raise HTTPException(400, why)
    if await _limited(f"signup:{email}", 5, 3600) or await _limited(f"signup-ip:{_client_ip(request)}", 30, 3600):
        raise HTTPException(429, "Too many attempts. Try again later.")
    row = await _login_row(db, email)
    if row is not None and row.verified_at is not None:
        text, page = mailer.render(
            "You already have a Lumio account",
            ["Someone (hopefully you) tried to sign up with this address, which already has an account.",
             "Sign in with your password, or reset it if you've forgotten it."],  # fmt: skip
            "Reset your password", f"{get_settings().public_web_url}/forgot-password",
        )
        await mailer.send(email, "You already have a Lumio account", text, page)
        return CHECK_EMAIL
    hashed = await passwords.hash_password(body.password)
    name = " ".join((body.name or "").split())[:200]
    if row is None:
        row = PasswordLogin(email=email, password_hash=hashed, display_name=name)
        db.add(row)
    else:
        row.password_hash, row.display_name = hashed, name or row.display_name
    await db.commit()
    text, page = mailer.render(
        "Confirm your email",
        ["Confirm this address to finish creating your Lumio account. The link works for 24 hours.",
         "If you didn't sign up, ignore this email."],  # fmt: skip
        "Confirm email", _link("/verify-email", make_token("verify", row)),
    )
    if not await mailer.send(email, "Confirm your email for Lumio", text, page):
        raise HTTPException(503, "We couldn't send the email right now. Try again in a few minutes.")
    return CHECK_EMAIL


class TokenIn(BaseModel):
    token: str


@router.post("/verify")
async def verify(body: TokenIn, request: Request, db: AsyncSession = Depends(get_db)):
    data = read_token("verify", body.token, VERIFY_TTL_S)
    row = await _login_row(db, str(data.get("e") or ""))
    if row is None or passwords.fingerprint(row.password_hash) != data.get("f"):
        raise HTTPException(400, "This link isn't valid any more. Sign up again or sign in.")
    if row.user_id is None:
        if not access.may_sign_up(row.email):  # sign-ups closed after the link was sent
            raise HTTPException(403, access.CLOSED_MESSAGE)
        user = User(email=row.email, display_name=row.display_name or row.email.split("@")[0], primary_provider="email",
                    timezone="Asia/Kolkata")  # fmt: skip
        db.add(user)
        await db.flush()
        row.user_id = user.id
        db.add(Account(user_id=user.id, provider="email", subject=row.email, email=row.email, scopes=""))
    if row.verified_at is None:
        row.verified_at = datetime.now(timezone.utc)
    await db.flush()
    user = await db.get(User, row.user_id)
    return await _signed_in(db, request, user)


class LoginIn(BaseModel):
    email: str
    password: str


@router.post("/login")
async def login(body: LoginIn, request: Request, db: AsyncSession = Depends(get_db)):
    _available()
    email = normalize_email(body.email)
    _enforced_sso(email)
    if await _limited(f"login:{email}", 20, 900) or await _limited(f"login-ip:{_client_ip(request)}", 60, 900):
        raise HTTPException(429, "Too many sign-in attempts. Wait a few minutes, or reset your password.")
    row = await _login_row(db, email)
    now = datetime.now(timezone.utc)
    if row is not None and row.locked_until and row.locked_until > now:
        raise HTTPException(429, "Too many wrong passwords. Wait 15 minutes, or reset your password.")
    ok = await passwords.verify_password(body.password, row.password_hash if row else None)
    if row is None or not ok:
        if row is not None:
            row.failed_count = (row.failed_count or 0) + 1
            if row.failed_count >= MAX_FAILURES:
                row.locked_until, row.failed_count = now + timedelta(minutes=LOCK_MINUTES), 0
            await db.commit()
        raise HTTPException(401, "Wrong email or password.")
    if row.verified_at is None or row.user_id is None:
        raise HTTPException(403, "Confirm your email first: open the link we sent you, or ask for a new one.")
    row.failed_count, row.locked_until = 0, None
    user = await db.get(User, row.user_id)
    if user is None:
        raise HTTPException(401, "Wrong email or password.")
    return await _signed_in(db, request, user)


class EmailIn(BaseModel):
    email: str


@router.post("/resend")
async def resend(body: EmailIn, db: AsyncSession = Depends(get_db)):
    """A new confirmation link for an address that signed up but hasn't confirmed."""
    _available()
    email = normalize_email(body.email)
    if await _limited(f"resend:{email}", 3, 3600):
        return CHECK_EMAIL
    row = await _login_row(db, email)
    if row is not None and row.verified_at is None:
        text, page = mailer.render("Confirm your email", ["Confirm this address to finish creating your Lumio account."],
                                   "Confirm email", _link("/verify-email", make_token("verify", row)))  # fmt: skip
        await mailer.send(email, "Confirm your email for Lumio", text, page)
    return CHECK_EMAIL


@router.post("/forgot")
async def forgot(body: EmailIn, request: Request, db: AsyncSession = Depends(get_db)):
    """Always the same answer. A verified password login gets a reset link; an address that signs in with Google,
    Microsoft or SSO is told so."""
    _available()
    email = normalize_email(body.email)
    if await _limited(f"forgot:{email}", 3, 3600) or await _limited(f"forgot-ip:{_client_ip(request)}", 20, 3600):
        return CHECK_EMAIL
    row = await _login_row(db, email)
    if row is not None and row.verified_at is not None:
        text, page = mailer.render(
            "Reset your Lumio password",
            ["Use this link to choose a new password. It works for one hour, once.",
             "If you didn't ask for this, ignore this email: your password stays the same."],  # fmt: skip
            "Choose a new password", _link("/reset-password", make_token("reset", row)),
        )
        await mailer.send(email, "Reset your Lumio password", text, page)
        return CHECK_EMAIL
    other = (await db.execute(select(Account.provider).where(Account.email.ilike(email), Account.provider != "email").limit(1))).scalar_one_or_none()
    if other:
        how = {"google": "Google", "microsoft": "Microsoft"}.get(other, "single sign-on")
        text, page = mailer.render(
            "How you sign in to Lumio",
            [f"This address signs in to Lumio with {how}, so there's no Lumio password to reset.",
             f"Open Lumio and choose Continue with {how}."],  # fmt: skip
            "Open Lumio", f"{get_settings().public_web_url}/login",
        )
        await mailer.send(email, "How you sign in to Lumio", text, page)
    return CHECK_EMAIL


class ResetIn(BaseModel):
    token: str
    password: str


@router.post("/reset")
async def reset(body: ResetIn, request: Request, db: AsyncSession = Depends(get_db)):
    data = read_token("reset", body.token, RESET_TTL_S)
    row = await _login_row(db, str(data.get("e") or ""))
    if row is None or row.user_id is None or passwords.fingerprint(row.password_hash) != data.get("f"):
        raise HTTPException(400, "This link has already been used or isn't valid. Ask for a new one.")
    why = passwords.problem(body.password, row.email)
    if why:
        raise HTTPException(400, why)
    row.password_hash = await passwords.hash_password(body.password)
    row.failed_count, row.locked_until = 0, None
    await _revoke_sessions(db, row.user_id)
    user = await db.get(User, row.user_id)
    resp = await _signed_in(db, request, user)
    await _changed_notice(row.email)
    return resp


class ChangeIn(BaseModel):
    current: str
    password: str


@router.post("/password")
async def change_password(body: ChangeIn, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(PasswordLogin).where(PasswordLogin.user_id == user.id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(400, "This account signs in with Google, Microsoft or SSO, not a password.")
    if not await passwords.verify_password(body.current, row.password_hash):
        raise HTTPException(401, "Your current password is wrong.")
    why = passwords.problem(body.password, row.email)
    if why:
        raise HTTPException(400, why)
    row.password_hash = await passwords.hash_password(body.password)
    await _revoke_sessions(db, user.id, keep=getattr(request.state, "auth_session_id", None))
    await db.commit()
    await _changed_notice(row.email)
    return {"ok": True}


async def _revoke_sessions(db: AsyncSession, user_id, keep=None) -> None:
    """Sign out every session of the user (but ``keep``), also from the session cache."""
    q = select(AuthSession.id).where(AuthSession.user_id == user_id, AuthSession.revoked.is_(False))
    ids = [sid for sid in (await db.execute(q)).scalars().all() if sid != keep]
    if not ids:
        return
    await db.execute(update(AuthSession).where(AuthSession.id.in_(ids)).values(revoked=True))
    try:
        from app.cache import get_redis

        r = get_redis()
        ttl = get_settings().session_ttl_days * 86400
        for sid in ids:
            await r.set(f"revoked:{sid}", b"1", ex=ttl)
            await r.delete(f"sess:{sid}")
    except Exception:  # noqa: BLE001 - the database flag still holds once the cache entry expires
        log.info("session cache not updated", exc_info=True)


async def _changed_notice(email: str) -> None:
    text, page = mailer.render(
        "Your Lumio password was changed",
        ["The password for this account was just changed, and other devices were signed out.",
         "If this wasn't you, reset your password now."],  # fmt: skip
        "Reset your password", f"{get_settings().public_web_url}/forgot-password",
    )
    await mailer.send(email, "Your Lumio password was changed", text, page)


async def _signed_in(db: AsyncSession, request: Request, user: User) -> JSONResponse:
    from app.auth.routes import new_session

    token = await new_session(db, request, user)
    resp = JSONResponse({"ok": True, "redirect": "/chat"})
    from app.auth.routes import _session_cookie

    _session_cookie(resp, token)
    return resp
