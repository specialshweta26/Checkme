"""Inbox checks are one job per user, against a real Postgres: one instance works a mailbox at a time, a mailbox is
checked again only when due, and a new-mail notification checks just that user's mailbox right away.

Skipped unless CHECKME_TEST_DATABASE_URL is set (see tests/test_user_memory_db.py).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from tests.test_user_memory_db import _user_with_agents, factory  # noqa: F401 - the fixture is used by name

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")


async def _mail_user(factory):
    from app.models import Connector

    user, _ = await _user_with_agents(factory)
    async with factory() as db:
        db.add(Connector(user_id=user.id, slug=f"outlook-{uuid.uuid4().hex[:6]}", name="Outlook", provider="outlook", kind="builtin", enabled=True))
        await db.commit()
    return user


@pytest.fixture
def mailbox(monkeypatch):
    from app.inbox import worker

    checked = []

    async def refresh(db, user, unread_only=False, classify=False):
        checked.append(user.id)
        await asyncio.sleep(0.05)  # long enough for a second instance to try at the same time
        return 1

    async def classify(db, user):
        return 0

    monkeypatch.setattr(worker, "refresh_mailboxes", refresh)
    monkeypatch.setattr(worker, "classify_pending", classify)
    worker._urgent.clear()
    return checked


async def test_two_instances_never_check_the_same_mailbox_at_once(factory, mailbox):
    from app.inbox import worker

    user = await _mail_user(factory)
    results = await asyncio.gather(*(worker.check_user(factory, user.id) for _ in range(5)))
    assert results.count(1) == 1 and results.count(None) == 4
    assert mailbox == [user.id]


async def test_a_mailbox_is_checked_again_only_when_due(factory, mailbox):
    from app.inbox import worker

    user = await _mail_user(factory)
    async with factory() as db:
        assert user.id in await worker.due_user_ids(db, limit=100_000)
    assert await worker.check_user(factory, user.id) == 1
    async with factory() as db:
        assert user.id not in await worker.due_user_ids(db, limit=100_000)  # checked just now
    assert await worker.check_user(factory, user.id) is None
    assert await worker.check_user(factory, user.id, urgent=True) == 1  # new mail: check now anyway


async def test_a_notification_checks_only_that_users_mailbox(factory, mailbox, monkeypatch):
    from app.inbox import worker

    a, b = await _mail_user(factory), await _mail_user(factory)
    for uid in (a.id, b.id):
        await worker.check_user(factory, uid)  # both checked recently: nothing is due
    mailbox.clear()

    async def nothing_due(db, limit=worker.USERS_PER_TICK):
        return []

    monkeypatch.setattr(worker, "due_user_ids", nothing_due)
    worker.wake_inbox({a.id})
    assert await worker.check_due(factory) == 1
    assert mailbox == [a.id]  # b's mailbox was not touched
    assert worker._urgent == set()
