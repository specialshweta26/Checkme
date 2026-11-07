"""The nightly pass keeps memory tidy, against a real Postgres: replaced facts retire, guesses fade, duplicate people
merge, cards are rewritten (never appended to), every change is logged, forget clears the copies, and one user's
pass never touches another user's memory.

Skipped unless CHECKME_TEST_DATABASE_URL is set (see tests/test_user_memory_db.py).
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.test_user_memory_db import _user_with_agents, factory  # noqa: F401 - the fixture is used by name

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")

NOW = datetime(2026, 11, 7, tzinfo=timezone.utc)


def test_confidence_fades_unless_confirmed():
    from app.memory_engine.consolidate import effective_confidence

    def fact(days, trust="inferred", reinforced=1, confidence=0.6):
        return SimpleNamespace(confidence=confidence, trust=trust, reinforced=reinforced, last_confirmed_at=None,
                               valid_from=NOW - timedelta(days=days), created_at=None)

    assert effective_confidence(fact(0), NOW) == 0.6
    assert effective_confidence(fact(90), NOW) == pytest.approx(0.3)  # a 90-day half-life
    assert effective_confidence(fact(90, reinforced=2), NOW) > effective_confidence(fact(90), NOW)  # confirmed lasts longer
    assert effective_confidence(fact(900, trust="user_said", confidence=0.9), NOW) == pytest.approx(0.45)  # never below half
    assert effective_confidence(fact(400), NOW) < 0.2  # an old guess falls below the fade line


async def _changes(db, user_id, op=None):
    from app.models import MemoryChange

    q = select(MemoryChange).where(MemoryChange.user_id == user_id)
    if op:
        q = q.where(MemoryChange.op == op)
    return (await db.execute(q)).scalars().all()


async def test_replaced_facts_retire_and_old_guesses_fade(factory):
    from app.agent import memory
    from app.memory_engine import consolidate
    from app.models import MemoryPassage

    user, (chief,) = await _user_with_agents(factory)
    async with factory() as db:
        a = await memory.remember(db, user.id, "archival", "User lives in Bangalore", chief.id, predicate="lives_in")
        b = await memory.remember(db, user.id, "archival", "User works at Acme", chief.id, predicate="works_at")
        guess = await memory.remember(db, user.id, "archival", "User may like cricket", chief.id, trust="inferred")
        # Older data: two active facts for the same thing (saved before supersede worked), and a stale guess.
        c = MemoryPassage(user_id=user.id, agent_id=chief.id, kind="archival", text="User lives in Pune", scope="user",
                          predicate="lives_in", trust="user_said", confidence=0.9, valid_from=NOW)
        db.add(c)
        a.valid_from = NOW - timedelta(days=30)
        guess.valid_from = guess.created_at = NOW - timedelta(days=500)
        await db.commit()
        assert await consolidate.retire_replaced(db, user.id) == 1
        assert await consolidate.fade(db, user.id) == 1
        await db.commit()
        rows = {r.id: r for r in (await db.execute(select(MemoryPassage).where(MemoryPassage.user_id == user.id))).scalars()}
        assert rows[a.id].status == "superseded" and rows[c.id].status == "active" and rows[b.id].status == "active"
        assert rows[guess.id].status == "faded"
        sup = await _changes(db, user.id, "supersede")
        assert sup[0].before == "User lives in Bangalore" and sup[0].after == "User lives in Pune"
        assert "cricket" in (await _changes(db, user.id, "fade"))[0].before
        assert {c.after for c in await _changes(db, user.id, "add")} >= {"User lives in Bangalore", "User works at Acme"}
        # A faded guess is no longer recalled.
        assert "cricket" not in await memory.load_memory(db, user, "Does the user like cricket?", chief)


async def test_a_person_known_by_name_merges_into_the_one_from_mail(factory):
    from app.agent import memory
    from app.memory_engine import consolidate
    from app.memory_engine.entities import resolve
    from app.models import MemoryEntity, MemoryPassage

    user, (chief,) = await _user_with_agents(factory)
    async with factory() as db:
        from_chat = await resolve(db, user.id, name="Priya Nair")  # chat: no address
        fact = await memory.remember(db, user.id, "archival", "Priya is the user's manager", chief.id,
                                     subject_entity_id=from_chat.id, predicate="role")
        await db.commit()
        # The learner can't link mail to the chat record (an alias on it would match), so make the mail one directly.
        from_mail = MemoryEntity(user_id=user.id, kind="person", name="Priya Nair", mail_in=4, meetings=2)
        db.add(from_mail)
        await db.flush()
        from app.memory_engine.entities import _claim_alias

        await _claim_alias(db, user.id, from_mail.id, "email", "priya@acme.com")
        other = await resolve(db, user.id, name="Priya Nair", email="priya.n@zeta.com")  # same name, another address
        await db.commit()
        assert other.id == from_chat.id  # the name alias matched the chat record
        from_mail_2 = MemoryEntity(user_id=user.id, kind="person", name="Rahul Mehta")
        db.add(from_mail_2)
        await db.commit()
        # from_chat now has an email too, so the two Priyas both have addresses: an ambiguity, left alone.
        assert await consolidate.merge_duplicates(db, user.id) == 0

    user2, (chief2,) = await _user_with_agents(factory)
    async with factory() as db:
        from_chat = await resolve(db, user2.id, name="Priya Nair")
        fact = await memory.remember(db, user2.id, "archival", "Priya is the user's manager", chief2.id,
                                     subject_entity_id=from_chat.id, predicate="role")
        from_mail = MemoryEntity(user_id=user2.id, kind="person", name="Priya  Nair", mail_in=4, meetings=2)
        db.add(from_mail)
        await db.flush()
        from app.memory_engine.entities import _claim_alias

        await _claim_alias(db, user2.id, from_mail.id, "email", "priya@acme.com")
        await db.commit()
        assert await consolidate.merge_duplicates(db, user2.id) == 1
        await db.commit()
        await db.refresh(from_chat)
        await db.refresh(from_mail)
        assert from_chat.status == "merged" and from_mail.status == "active" and from_mail.mail_in == 4
        assert (await db.get(MemoryPassage, fact.id)).subject_entity_id == from_mail.id
        assert (await resolve(db, user2.id, name="Priya Nair")).id == from_mail.id  # the name now finds the merged one
        assert (await _changes(db, user2.id, "merge"))[0].target == f"entity:{from_chat.id}"


async def test_cards_are_rewritten_from_facts_and_used_in_recall(factory, monkeypatch):
    from app.agent import memory
    from app.memory_engine import consolidate
    from app.memory_engine.entities import people_context, resolve

    prompts = []

    async def fake_llm(prompt, max_tokens):
        prompts.append(prompt)
        return "Priya Nair: COO at Zeta Corp and the user's manager; prefers short bullet updates."

    monkeypatch.setattr(consolidate, "_llm", fake_llm)
    user, (chief,) = await _user_with_agents(factory)
    async with factory() as db:
        priya = await resolve(db, user.id, name="Priya Nair", email="priya@zeta.com")
        await memory.remember(db, user.id, "archival", "Priya is COO at Zeta Corp", chief.id, subject_entity_id=priya.id, predicate="role")
        await memory.remember(db, user.id, "archival", "Priya prefers short bullet updates", chief.id, subject_entity_id=priya.id)
        assert await consolidate.rewrite_cards(db, user.id) == 1
        await db.commit()
        assert "COO at Zeta Corp (the user said)" in prompts[0] and "data, not instructions" in prompts[0]
        assert await consolidate.rewrite_cards(db, user.id) == 0  # nothing changed: no rewrite, no model call
        lines = await people_context(db, user.id, "What does Priya prefer?")
        assert "Card: Priya Nair: COO at Zeta Corp" in lines[0]
        assert (await _changes(db, user.id, "card"))[0].after.startswith("Priya Nair: COO")
        # Forget clears the card and every logged copy of the text.
        await memory._forget_everywhere(db, user, chief, "Priya Nair")
        await db.commit()
        assert not [c for c in await _changes(db, user.id) if "priya" in (c.before + c.after).lower()]


async def test_the_pass_runs_once_a_day_per_user_and_only_on_their_memory(factory, monkeypatch):
    from app.agent import memory
    from app.memory_engine import consolidate
    from app.models import MemoryPassage

    async def no_llm(prompt, max_tokens):
        return ""

    monkeypatch.setattr(consolidate, "_llm", no_llm)
    a, (chief_a,) = await _user_with_agents(factory)
    b, (chief_b,) = await _user_with_agents(factory)
    async with factory() as db:
        for user, chief in ((a, chief_a), (b, chief_b)):
            g = await memory.remember(db, user.id, "archival", f"User may like chess {user.id}", chief.id, trust="inferred")
            g.valid_from = NOW - timedelta(days=500)
        await db.commit()
    results = await asyncio.gather(*(consolidate.run_user(factory, a.id) for _ in range(4)))
    assert sum(1 for r in results if r is not None) == 1  # one instance wins the lease
    assert await consolidate.run_user(factory, a.id) is None  # ran today: not due
    async with factory() as db:
        assert a.id not in await consolidate.due_users(db, limit=10_000)
        status = {r.user_id: r.status for r in (await db.execute(
            select(MemoryPassage).where(MemoryPassage.user_id.in_([a.id, b.id]), MemoryPassage.text.like("User may like chess%")))).scalars()}
        assert status == {a.id: "faded", b.id: "active"}  # b's memory untouched by a's pass
