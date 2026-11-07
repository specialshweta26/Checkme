"""Inbox sync and triage, one job per user.

Each tick asks the database which users' inboxes are due (checked more than INTERVAL ago) and works on them in
parallel, each under a per-user lease in memory_learner_state (job "inbox"), so with several API instances a mailbox is
synced and triaged by one of them only, never twice. A new-mail notification (Microsoft Graph push) queues just that
user's mailbox for an immediate check instead of a sweep of everyone's.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from app.db import SessionLocal
from app.inbox.service import classify_pending, refresh_mailboxes
from app.models import Connector, User

log = logging.getLogger(__name__)
INTERVAL_SEC = 90
INTERVAL = timedelta(seconds=INTERVAL_SEC)
WAKE_GAP_SEC = 5  # new mail often comes in bursts, so let them collect into one check
JOB = "inbox"
LEASE = timedelta(minutes=3)  # a check takes seconds; a stopped instance's lease frees up quickly
PARALLEL_USERS = 8  # mailbox and model calls are I/O: several users at once
USERS_PER_TICK = 200
_wake = asyncio.Event()
_urgent: set = set()  # users whose mailbox reported new mail since the last tick


def wake_inbox(user_ids=None) -> None:
    """A mailbox reported new mail: check those users now rather than at their next turn."""
    if user_ids:
        _urgent.update(user_ids)
    _wake.set()


async def _wait_for_next_check() -> None:
    try:
        await asyncio.wait_for(_wake.wait(), timeout=INTERVAL_SEC)
        await asyncio.sleep(WAKE_GAP_SEC)
    except asyncio.TimeoutError:
        pass
    _wake.clear()


async def _mail_user_ids() -> list:
    """Every user with a connected mailbox (the learner uses this too)."""
    async with SessionLocal() as db:
        return list(
            (
                await db.execute(
                    select(Connector.user_id)
                    .where(Connector.enabled.is_(True), Connector.provider.in_(("gmail", "outlook")))
                    .distinct()
                )
            )
            .scalars()
            .all()
        )


async def due_user_ids(db, limit: int = USERS_PER_TICK) -> list:
    """Users with a mailbox whose inbox check is due and not held by another instance; longest-waiting first."""
    now = datetime.now(timezone.utc)
    rows = await db.execute(text(
        "SELECT c.user_id FROM (SELECT DISTINCT user_id FROM connectors "
        "    WHERE enabled AND provider IN ('gmail', 'outlook')) c "
        "LEFT JOIN memory_learner_state s ON s.user_id = c.user_id AND s.job = :job "
        "WHERE (s.last_run_at IS NULL OR s.last_run_at < :due) AND (s.lease_until IS NULL OR s.lease_until < :now) "
        "ORDER BY s.last_run_at NULLS FIRST LIMIT :lim"
    ), {"job": JOB, "due": now - INTERVAL, "now": now, "lim": limit})
    return [r[0] for r in rows.all()]


async def check_user(session_factory, user_id, *, urgent: bool = False) -> int | None:
    """Sync and triage one user's inbox under their lease. None when another instance has it or it isn't due."""
    from app.memory_engine.learn import claim_job, finish_job

    async with session_factory() as db:
        if not await claim_job(db, user_id, JOB, timedelta(0) if urgent else INTERVAL - timedelta(seconds=10), LEASE):
            return None
        error, arrived = "", 0
        try:
            user = await db.get(User, user_id)
            if user is not None:
                arrived = await asyncio.wait_for(refresh_mailboxes(db, user, unread_only=False, classify=False), timeout=45)
                await asyncio.wait_for(classify_pending(db, user), timeout=90)
        except Exception as exc:  # noqa: BLE001 - one user's mailbox must not stop the others
            error = f"{type(exc).__name__}: {exc}"
            log.info("inbox check failed for user %s", user_id, exc_info=True)
            await db.rollback()
        await finish_job(db, user_id, JOB, error)
        return arrived


async def check_due(session_factory=None) -> int:
    """One tick: users who reported new mail first, then everyone due, PARALLEL_USERS at a time."""
    session_factory = session_factory or SessionLocal
    urgent = set(_urgent)
    _urgent.difference_update(urgent)
    async with session_factory() as db:
        due = await due_user_ids(db)
    order = list(urgent) + [u for u in due if u not in urgent]
    gate = asyncio.Semaphore(PARALLEL_USERS)

    async def one(uid):
        async with gate:
            return await check_user(session_factory, uid, urgent=uid in urgent)

    results = await asyncio.gather(*(one(uid) for uid in order), return_exceptions=True)
    return sum(r for r in results if isinstance(r, int))


async def _check_mail_triggers() -> int:
    from app.automations.triggers import check_mail

    try:
        return await check_mail(SessionLocal)
    except Exception:  # noqa: BLE001 - triggers must not stop inbox sync
        log.exception("mail triggers failed")
        return 0


async def inbox_loop() -> None:
    await asyncio.sleep(30)
    while True:
        try:
            count = await check_due()
            if count:
                log.info("inbox worker stored %s new messages", count)
            if await _check_mail_triggers():
                from app.jobs.worker import wake

                wake()
        except Exception:  # noqa: BLE001
            log.exception("inbox worker tick failed")
        await _wait_for_next_check()


async def start_inbox_worker() -> asyncio.Task:
    return asyncio.create_task(inbox_loop(), name="inbox-sync")
