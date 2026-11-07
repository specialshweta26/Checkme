"""Microsoft Graph tells us when new mail arrives, so the inbox check runs within seconds.

A notification only means "check now": it wakes the normal inbox check, which keeps running on its timer in case a
notification is missed or a subscription lapses. Anyone can call the public endpoint, so a notification only counts if
it carries the secret given to Graph for that subscription, and all a fake one could cause is an extra check.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from connector_core.http import ApiClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Connector, MailSubscription

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
RESOURCE = "me/mailFolders('Inbox')/messages"
NOTIFY_PATH = "/api/webhooks/microsoft-mail"
LIFETIME = timedelta(days=2)  # within Graph's limit for mail subscriptions
RENEW_WITHIN = timedelta(days=1)
SYNC_EVERY_S = 30 * 60
MAX_BODY_BYTES = 256 * 1024


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _expiry(now: datetime) -> str:
    return (now + LIFETIME).strftime("%Y-%m-%dT%H:%M:%SZ")


async def accept_notifications(db: AsyncSession, payload: Any) -> list:
    """The users whose notifications carried the right secret for a known subscription (one entry per notification)."""
    items = payload.get("value") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    valid: list = []
    for item in items[:100]:
        if not isinstance(item, dict):
            continue
        row = (
            await db.execute(
                select(MailSubscription).where(MailSubscription.subscription_id == str(item.get("subscriptionId") or ""))
            )
        ).scalar_one_or_none()
        if row is not None and hmac.compare_digest(_hash(str(item.get("clientState") or "")), row.client_state_hash):
            valid.append(row.user_id)
    return valid


def _client(connector: Any, http: Any = None) -> ApiClient:
    from app.auth.tokens import account_access_token

    account_id = connector.account_id

    async def token() -> str:
        return await account_access_token(account_id)

    return ApiClient("Outlook", token, http)


async def _subscribe(api: ApiClient, db: AsyncSession, connector: Any, url: str, now: datetime) -> None:
    secret = secrets.token_urlsafe(32)
    made = await api.json(
        "POST",
        f"{GRAPH}/subscriptions",
        json={
            "changeType": "created",
            "notificationUrl": url,
            "resource": RESOURCE,
            "expirationDateTime": _expiry(now),
            "clientState": secret,
        },
    )
    db.add(
        MailSubscription(
            connector_id=connector.id,
            user_id=connector.user_id,
            subscription_id=made["id"],
            client_state_hash=_hash(secret),
            expires_at=now + LIFETIME,
        )
    )
    await db.commit()


async def sync_connector(db: AsyncSession, connector: Any, url: str, now: datetime, http: Any = None) -> str:
    """Make sure one mailbox has a live subscription. Returns what it did."""
    api = _client(connector, http)
    row = (
        await db.execute(select(MailSubscription).where(MailSubscription.connector_id == connector.id))
    ).scalar_one_or_none()
    if row is None:
        await _subscribe(api, db, connector, url, now)
        return "created"
    if row.expires_at - now > RENEW_WITHIN:
        return "ok"
    try:
        await api.json("PATCH", f"{GRAPH}/subscriptions/{row.subscription_id}", json={"expirationDateTime": _expiry(now)})
    except Exception as exc:  # noqa: BLE001 - usually already expired on Graph's side, so make a new one
        log.info("mail subscription renewal failed: %s", type(exc).__name__)
        await db.delete(row)
        await db.commit()
        await _subscribe(api, db, connector, url, now)
        return "recreated"
    row.expires_at = now + LIFETIME
    await db.commit()
    return "renewed"


async def sync_all(session_factory: Callable, now: datetime | None = None, http: Any = None) -> int:
    """Subscribe or renew every connected Outlook mailbox. A failure is logged, and the timer still finds the mail."""
    cfg = get_settings()
    if not cfg.microsoft_mail_push or not cfg.public_web_url.startswith("https://"):
        return 0  # Graph only calls https addresses it can reach
    url = cfg.public_web_url + NOTIFY_PATH
    now = now or datetime.now(timezone.utc)
    async with session_factory() as db:
        connectors = (
            await db.execute(
                select(Connector).where(
                    Connector.provider == "outlook",
                    Connector.kind == "builtin",
                    Connector.enabled.is_(True),
                    Connector.account_id.is_not(None),
                )
            )
        ).scalars().all()
    done = 0
    for connector in connectors:
        try:
            async with session_factory() as db:
                await sync_connector(db, connector, url, now, http)
            done += 1
        except Exception as exc:  # noqa: BLE001 - one mailbox must not stop the others
            log.warning("mail subscription for connector %s failed: %s: %s", connector.id, type(exc).__name__, str(exc)[:200])
    return done


async def push_loop(session_factory: Callable | None = None) -> None:
    if session_factory is None:
        from app.db import SessionLocal

        session_factory = SessionLocal
    await asyncio.sleep(60)
    while True:
        try:
            await sync_all(session_factory)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("mail subscription sync failed")
        await asyncio.sleep(SYNC_EVERY_S)


async def start_mail_push() -> asyncio.Task:
    return asyncio.create_task(push_loop(), name="mail-push")
