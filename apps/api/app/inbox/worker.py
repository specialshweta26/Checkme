from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from app.db import SessionLocal
from app.inbox.service import refresh_mailboxes
from app.models import Connector, User

log = logging.getLogger(__name__)
INTERVAL_SEC = 20


async def sync_all_inboxes() -> int:
    """Pull new mail for every connected Gmail/Outlook account. Does not need a browser session."""
    arrived = 0
    async with SessionLocal() as db:
        user_ids = list(
            (
                await db.execute(
                    select(Connector.user_id)
                    .where(
                        Connector.enabled.is_(True),
                        Connector.provider.in_(("gmail", "outlook")),
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
    for user_id in user_ids:
        try:
            async with SessionLocal() as db:
                user = await db.get(User, user_id)
                if user is None:
                    continue
                arrived += await refresh_mailboxes(db, user, unread_only=True, classify=True)
        except Exception:  # noqa: BLE001
            log.exception("inbox sync failed for user %s", user_id)
    return arrived


async def inbox_loop() -> None:
    await asyncio.sleep(6)
    while True:
        try:
            count = await sync_all_inboxes()
            if count:
                log.info("inbox worker stored %s new messages", count)
        except Exception:  # noqa: BLE001
            log.exception("inbox worker tick failed")
        await asyncio.sleep(INTERVAL_SEC)


async def start_inbox_worker() -> asyncio.Task:
    return asyncio.create_task(inbox_loop(), name="inbox-sync")
