"""Saved sign-ins for the browser (docs/BROWSING.md "Saved sign-ins").

The user adds a site's username and password on the Saved runs page; they never pass through chat or a model.
Both are encrypted with the app key (``app.security.encrypt``). When a browser task or saved run on that site
needs them, code adds ``login_username`` / ``login_password`` to the run's values: the planner sees only the
``{{names}}``, the daemon types the values, and results and logs are scrubbed. CAPTCHAs and one-time codes still go
to the user.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from app.security import decrypt, encrypt

log = logging.getLogger(__name__)

USERNAME, PASSWORD = "login_username", "login_password"
MAX_VALUE = 500


def normalize_host(raw: str) -> str:
    """'https://www.Example.com/login' or 'example.com' -> 'example.com'."""
    from app.agent.fast_browser import host_of

    text = str(raw or "").strip()
    if not text:
        return ""
    return host_of(text if "://" in text else f"https://{text}")


def hint(username: str) -> str:
    """Enough of a username to recognise it, never the whole thing: 'g•••@gmail.com', 'ad•••'."""
    name = str(username or "")
    if "@" in name:
        local, _, domain = name.partition("@")
        return f"{local[:1]}•••@{domain}"
    return f"{name[:2]}•••" if name else ""


def _host_matches(saved: str, host: str) -> bool:
    """A login for example.com also covers its subdomains (app.example.com), never the other way round."""
    return bool(saved and host) and (host == saved or host.endswith("." + saved))


def _row_out(row) -> dict:
    return {
        "id": str(row.id), "host": row.host, "label": row.label or row.host, "username_hint": hint(decrypt(row.username_enc) or ""),
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }  # fmt: skip


async def list_logins(user_id) -> list[dict]:
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import SiteLogin

    async with SessionLocal() as db:
        q = select(SiteLogin).where(SiteLogin.user_id == uuid.UUID(str(user_id))).order_by(SiteLogin.host)
        return [_row_out(r) for r in (await db.execute(q)).scalars().all()]


async def save_login(user_id, host: str, username: str, password: str, label: str = "") -> dict:
    """Add or replace the sign-in for a site. An empty password keeps the saved one (rename / new username)."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import SiteLogin

    site = normalize_host(host)
    if not site or "." not in site:
        raise ValueError("give the site address, for example portal.example.com")
    username = str(username or "").strip()
    if not username or len(username) > MAX_VALUE or len(str(password or "")) > MAX_VALUE:
        raise ValueError("a username (and password) of at most 500 characters is needed")
    uid = uuid.UUID(str(user_id))
    async with SessionLocal() as db:
        row = (await db.execute(select(SiteLogin).where(SiteLogin.user_id == uid, SiteLogin.host == site))).scalars().first()
        if row is None:
            if not password:
                raise ValueError("a password is needed for a new sign-in")
            row = SiteLogin(user_id=uid, host=site)
            db.add(row)
        row.username_enc = encrypt(username)
        if password:
            row.password_enc = encrypt(str(password))
        row.label = str(label or "").strip()[:200]
        await db.commit()
        return _row_out(row)


async def delete_login(user_id, login_id: str) -> bool:
    from app.db import SessionLocal
    from app.models import SiteLogin

    async with SessionLocal() as db:
        try:
            row = await db.get(SiteLogin, uuid.UUID(str(login_id)))
        except (TypeError, ValueError):
            return False
        if row is None or str(row.user_id) != str(user_id):
            return False
        await db.delete(row)
        await db.commit()
        return True


async def values_for(user_id, host: str) -> dict[str, str]:
    """{login_username, login_password} for the saved sign-in that covers this site, or {}. Best effort."""
    site = normalize_host(host)
    if user_id is None or not site:
        return {}
    try:
        from sqlalchemy import select

        from app.db import SessionLocal
        from app.models import SiteLogin

        async with SessionLocal() as db:
            rows = (await db.execute(select(SiteLogin).where(SiteLogin.user_id == uuid.UUID(str(user_id))))).scalars().all()
            hits = sorted((r for r in rows if _host_matches(r.host, site)), key=lambda r: len(r.host), reverse=True)
            if not hits:
                return {}
            row = hits[0]
            username, password = decrypt(row.username_enc), decrypt(row.password_enc)
            if not username or not password:
                return {}
            row.last_used_at = datetime.now(timezone.utc)
            await db.commit()
            return {USERNAME: username, PASSWORD: password}
    except Exception as exc:  # noqa: BLE001
        log.info("saved sign-in lookup skipped: %s", type(exc).__name__)
        return {}


async def sites_with_logins(user_id) -> list[str]:
    try:
        return [r["host"] for r in await list_logins(user_id)]
    except Exception:  # noqa: BLE001
        return []
