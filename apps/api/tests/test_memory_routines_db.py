"""Routines driven by memory, against a real Postgres: who to reconnect with, and meeting prep from what memory knows
about the attendees — only this user's memory, and without adding strangers to it.

Skipped unless CHECKME_TEST_DATABASE_URL is set (see tests/test_user_memory_db.py).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from tests.test_user_memory_db import _user_with_agents, factory  # noqa: F401 - the fixture is used by name

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")

NOW = datetime(2026, 11, 7, 9, tzinfo=timezone.utc)


async def _person(db, user_id, name, email, days_ago, mail_out=0, meetings=0, card=""):
    from app.memory_engine.entities import resolve

    e = await resolve(db, user_id, name=name, email=email)
    e.mail_out, e.meetings, e.summary = mail_out, meetings, card
    e.last_interaction_at = NOW - timedelta(days=days_ago) if days_ago is not None else None
    return e


async def test_reconnect_lists_quiet_people_the_user_works_with(factory):
    from app.memory_engine.routines import reconnect

    a, _ = await _user_with_agents(factory)
    b, _ = await _user_with_agents(factory)
    async with factory() as db:
        await _person(db, a.id, "Priya Nair", "priya@acme.com", 30, mail_out=12, meetings=3)
        await _person(db, a.id, "Rahul Mehta", "rahul@acme.com", 40, mail_out=4, meetings=1)
        await _person(db, a.id, "Sam Lee", "sam@acme.com", 2, mail_out=20)  # talked recently
        await _person(db, a.id, "Old Friend", "old@acme.com", 400, mail_out=30)  # too long ago
        await _person(db, a.id, "Once", "once@acme.com", 30, mail_out=1)  # barely knows them
        await _person(db, b.id, "B Contact", "bc@zeta.com", 30, mail_out=50)  # another user's
        await db.commit()
        quiet = await reconnect(db, a.id, NOW)
        assert [q["name"] for q in quiet] == ["Priya Nair", "Rahul Mehta"]
        assert quiet[0]["days_quiet"] == 30


async def test_meeting_prep_uses_memory_and_adds_no_one(factory):
    from app.agent import memory
    from app.memory_engine.routines import meeting_prep, prep_block
    from app.models import MemoryEntity

    a, (chief,) = await _user_with_agents(factory)
    async with factory() as db:
        priya = await _person(db, a.id, "Priya Nair", "priya@acme.com", 3, mail_out=12, meetings=4,
                              card="Head of Ops at Acme; prefers bullet summaries")
        rahul = await _person(db, a.id, "Rahul Mehta", "rahul@acme.com", 10, mail_out=2)
        await db.commit()
        await memory.remember(db, a.id, "archival", "The user promised Priya Nair: send the revised SOW (due Friday)", chief.id,
                              subject_entity_id=priya.id)
        await memory.remember(db, a.id, "archival", "Rahul runs the Pune warehouse", chief.id, subject_entity_id=rahul.id)
        before = (await db.execute(select(func.count()).select_from(MemoryEntity).where(MemoryEntity.user_id == a.id))).scalar()
        event = {"title": "Ops review", "attendees": [f"Me <{a.email}>", "Priya Nair <priya@acme.com>",
                                                       "Rahul Mehta <rahul@acme.com>", "Stranger <new@other.com>"]}
        lines = await meeting_prep(db, a, event, NOW)
        assert len(lines) == 2  # the user and the stranger are left out
        assert lines[0].startswith("Priya Nair: Head of Ops at Acme") and "last contact 3 days ago" in lines[0]
        assert "Open: send the revised SOW (due Friday)." in lines[0]
        assert lines[1].startswith("Rahul Mehta: Rahul runs the Pune warehouse.")
        after = (await db.execute(select(func.count()).select_from(MemoryEntity).where(MemoryEntity.user_id == a.id))).scalar()
        assert after == before  # no record made for the stranger
        assert prep_block(lines).startswith("What you know about the attendees (from the user's memory; reference only")
        assert prep_block([]) == ""
