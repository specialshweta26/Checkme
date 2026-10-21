from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from app.db import SessionLocal
from app.inbox.service import classify_pending, refresh_mailboxes
from app.models import Connector, User

log = logging.getLogger(__name__)
INTERVAL_SEC = 8
_classify_task: asyncio.Task | None = None


async def _mail_user_ids() -> list:
    async with SessionLocal() as db:
        return list(
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


async def ingest_all_inboxes() -> int:
    """Store new mail for every connected account. Does not need a browser session."""
    arrived = 0
    for user_id in await _mail_user_ids():
        try:
            async with SessionLocal() as db:
                user = await db.get(User, user_id)
                if user is None:
                    continue
                arrived += await asyncio.wait_for(
                    refresh_mailboxes(db, user, unread_only=False, classify=False),
                    timeout=45,
                )
        except Exception:  # noqa: BLE001
            log.exception("inbox ingest failed for user %s", user_id)
    return arrived


async def classify_all_inboxes() -> None:
    for user_id in await _mail_user_ids():
        try:
            async with SessionLocal() as db:
                user = await db.get(User, user_id)
                if user is None:
                    continue
                await asyncio.wait_for(classify_pending(db, user), timeout=90)
        except Exception:  # noqa: BLE001
            log.exception("inbox classify failed for user %s", user_id)


async def inbox_loop() -> None:
    global _classify_task
    await asyncio.sleep(2)
    while True:
        try:
            count = await ingest_all_inboxes()
            if count:
                log.info("inbox worker stored %s new messages", count)
        except Exception:  # noqa: BLE001
            log.exception("inbox worker tick failed")
        if _classify_task is None or _classify_task.done():
            _classify_task = asyncio.create_task(classify_all_inboxes(), name="inbox-classify")
        await asyncio.sleep(INTERVAL_SEC)


async def start_inbox_worker() -> asyncio.Task:
    return asyncio.create_task(inbox_loop(), name="inbox-sync")
