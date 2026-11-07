"""Learning people, meetings, voice and promises from mail and calendar, against a real Postgres.

Skipped unless CHECKME_TEST_DATABASE_URL is set (see tests/test_user_memory_db.py).
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.test_user_memory_db import _user_with_agents, factory  # noqa: F401 - the fixture is used by name

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")


class FakeMailbox:
    """Answers the mailbox calls the learner makes, and records them."""

    slug = "outlook"

    def __init__(self, me: str, inbox=(), sent=(), events=(), bodies=None):
        self.me, self.inbox, self.sent, self.events, self.bodies = me, list(inbox), list(sent), list(events), bodies or {}
        self.calls = []

    async def call(self, tool, args):
        self.calls.append(tool)
        if tool == "list_messages":
            return {"messages": self.inbox if args.get("folder") == "inbox" else self.sent}
        if tool == "list_events":
            return {"events": self.events}
        if tool == "get_message":
            return {"body": self.bodies.get(args.get("message_id"), "")}
        raise AssertionError(tool)


def _msg(mid, frm="", to="", cc="", date="Nov 06, 2026 10:00 AM", subject="Hi", snippet=""):
    return {"message_id": mid, "from": frm, "to": to, "cc": cc, "date": date, "subject": subject, "snippet": snippet}


async def _email_user(factory, email):
    from app.models import User

    user, agents = await _user_with_agents(factory)
    async with factory() as db:
        row = await db.get(User, user.id)
        row.email = email
        await db.commit()
        user.email = email
    return user, agents


async def test_people_are_learned_from_headers_once_and_per_user(factory):
    from app.memory_engine import learn
    from app.models import MemoryEntity

    a, _ = await _email_user(factory, f"gaurav{os.getpid()}a@agilitas.test")
    b, _ = await _email_user(factory, f"gaurav{os.getpid()}b@agilitas.test")
    box = FakeMailbox(
        a.email,
        inbox=[
            _msg("m1", frm="Priya Nair <priya@acme.com>", to=a.email),
            _msg("m2", frm="Sophos <do-not-reply@cloud.sophos.com>", to=a.email),
            _msg("m3", frm=f"Me <{a.email}>", to="priya@acme.com"),
        ],
        sent=[_msg("s1", frm=a.email, to="Priya Nair <priya@acme.com>", cc="Rahul Mehta <rahul@acme.com>, updates@acme.com")],
    )
    async with factory() as db:
        first = await learn.learn_mail(db, a, box)
        again = await learn.learn_mail(db, a, box)  # the same messages are not counted twice
        assert first == {"mail_in": 1, "mail_out": 2} and again == {"mail_in": 0, "mail_out": 0}
        people = {e.name: e for e in (await db.execute(select(MemoryEntity).where(MemoryEntity.user_id == a.id, MemoryEntity.kind == "person"))).scalars()}
        assert set(people) == {"Priya Nair", "Rahul Mehta"}  # no bulk senders, not the user themselves
        assert (people["Priya Nair"].mail_in, people["Priya Nair"].mail_out) == (1, 1)
        assert people["Priya Nair"].salience > people["Rahul Mehta"].salience > 0
        await learn.learn_mail(db, b, FakeMailbox(b.email, inbox=[_msg("m1", frm="Priya Nair <priya@acme.com>")]))
        b_priya = (await db.execute(select(MemoryEntity).where(MemoryEntity.user_id == b.id, MemoryEntity.name == "Priya Nair"))).scalar_one()
        assert b_priya.id != people["Priya Nair"].id and (b_priya.mail_in, b_priya.mail_out) == (1, 0)


async def test_meetings_and_who_meets_together_come_from_the_calendar(factory):
    from app.memory_engine import learn
    from app.memory_engine.entities import people_context
    from app.models import MemoryRelation

    a, _ = await _email_user(factory, f"cal{os.getpid()}@agilitas.test")
    events = [
        {"event_id": "e1", "title": "Ops review", "start": "Nov 05, 2026 03:00 PM",
         "attendees": [f"Me <{a.email}>", "Priya Nair <priya@acme.com>", "Rahul Mehta <rahul@acme.com>"], "organizer": "priya@acme.com"},
        {"event_id": "e2", "title": "Canceled: Standup", "start": "Nov 06, 2026 12:30 PM", "attendees": ["sam@acme.com"]},
        {"event_id": "e3", "title": "1:1", "start": "Nov 06, 2026 04:00 PM", "attendees": ["Priya Nair <priya@acme.com>"]},
    ]
    box = FakeMailbox(a.email, events=events)
    async with factory() as db:
        assert await learn.learn_calendar(db, a, box) == {"events": 2}
        assert await learn.learn_calendar(db, a, box) == {"events": 0}
        rel = (await db.execute(select(MemoryRelation).where(MemoryRelation.user_id == a.id))).scalars().all()
        assert len(rel) == 1 and rel[0].count == 1  # Priya and Rahul met together once
        lines = await people_context(db, a.id, "When did I last meet Priya?")
        assert len(lines) == 1 and "Priya Nair (Acme)" in lines[0] and "2 meetings" in lines[0] and "often meets with Rahul Mehta" in lines[0]
        top = await people_context(db, a.id, "Who do I meet with the most?")
        assert top[0].startswith("Priya Nair")
        top = await people_context(db, a.id, "Who do I email and meet with the most? Top 5")
        assert top[0].startswith("Priya Nair") and any(l.startswith("Rahul Mehta") for l in top)


async def test_a_job_runs_once_at_a_time_and_only_when_due(factory):
    from datetime import timedelta

    from app.memory_engine import learn

    a, _ = await _email_user(factory, f"lease{os.getpid()}@agilitas.test")

    async def claim():
        async with factory() as db:
            return await learn.claim_job(db, a.id, "people_mail", timedelta(minutes=15))

    results = await asyncio.gather(*(claim() for _ in range(5)))
    assert results.count(True) == 1  # one instance wins
    async with factory() as db:
        await learn.finish_job(db, a.id, "people_mail")
    assert await claim() is False  # ran just now: not due for 15 minutes


async def test_voice_and_promises_are_learned_from_sent_mail(factory, monkeypatch):
    from app.agent import memory
    from app.memory_engine import learn

    a, (chief,) = await _email_user(factory, f"voice{os.getpid()}@agilitas.test")
    sent = [_msg(f"c{i}", to=f"Colleague {i} <c{i}@agilitas.test>", subject=f"Update {i}", snippet="Will send the deck by Friday") for i in range(3)]
    sent += [_msg(f"x{i}", to="Priya Nair <priya@acme.com>", subject="Proposal", snippet="I will share the revised SOW by Friday") for i in range(3)]
    box = FakeMailbox(a.email, sent=sent, bodies={m["message_id"]: "Hi team,\nQuick update.\nThanks,\nG" for m in sent})

    async def fake_llm(prompt, max_tokens, json_mode=False):
        if json_mode:
            return json.dumps({"commitments": [{"to_name": "Priya Nair", "to_email": "priya@acme.com", "what": "share the revised SOW", "due": "Friday"}]})
        return "Opens with 'Hi team'\nShort, 3-4 lines\nSigns off 'Thanks, G'"

    monkeypatch.setattr(learn, "_llm", fake_llm)
    async with factory() as db:
        assert (await learn.learn_voice(db, a, box))["voice"] == 2
        assert (await learn.learn_commitments(db, a, box))["commitments"] == 1
        seen = await memory.load_memory(db, a, "What did I promise Priya, and how do I write to colleagues?", chief)
        assert "voice_colleagues: Opens with 'Hi team'" in seen
        assert "The user promised Priya Nair: share the revised SOW (due Friday)" in seen


async def test_run_user_runs_due_jobs_and_survives_a_failing_mailbox(factory, monkeypatch):
    from app.memory_engine import learn
    from app.models import MemoryLearnerState

    a, _ = await _email_user(factory, f"run{os.getpid()}@agilitas.test")

    class Broken(FakeMailbox):
        async def call(self, tool, args):
            raise RuntimeError("mailbox down")

    async def connectors(db, user):
        return [Broken(a.email)]

    monkeypatch.setattr("app.inbox.service.mail_connectors", connectors)

    async def no_llm(*a, **k):
        return "{}"

    monkeypatch.setattr(learn, "_llm", no_llm)
    await learn.run_user(factory, a.id)
    async with factory() as db:
        states = {s.job: s for s in (await db.execute(select(MemoryLearnerState).where(MemoryLearnerState.user_id == a.id))).scalars()}
        assert set(states) == set(learn.JOBS)
        assert all(s.last_run_at is not None and s.lease_until is None for s in states.values())  # leases released


async def test_the_users_other_addresses_are_not_contacts(factory):
    from app.memory_engine import learn
    from app.memory_engine.entities import mark_lookalikes, people_context, resolve, set_self
    from app.models import MemoryEntity, User

    a, _ = await _email_user(factory, f"gaurav{os.getpid()}self@agilitas.test")
    async with factory() as db:
        row = await db.get(User, a.id)
        row.display_name = "Gaurav R"
        await db.commit()
        a.display_name = "Gaurav R"
        # Learned before this check existed: the user's personal Gmail, counted as a contact.
        old = await resolve(db, a.id, name="gaurav grv", email="gaurav.grv@gmail.com")
        old.mail_out, old.salience = 23, 9.0
        await db.commit()
        assert await mark_lookalikes(db, a) == 1
        box = FakeMailbox(a.email, sent=[
            _msg("s1", frm=f"Gaurav R <{a.email}>", to="gaurav grv <gaurav.grv@gmail.com>, Gaurav Sharma <gaurav.sharma@acme.com>"),
            _msg("s2", frm="Gaurav R <g.r@agilitas-alias.test>", to="Priya Nair <priya@acme.com>"),  # a send-as alias
        ], inbox=[_msg("m1", frm="Gaurav R <g.r@agilitas-alias.test>", to=a.email)])
        assert await learn.learn_mail(db, a, box) == {"mail_in": 0, "mail_out": 2}  # Sharma and Priya only
        people = {e.name: e for e in (await db.execute(select(MemoryEntity).where(MemoryEntity.user_id == a.id, MemoryEntity.kind == "person"))).scalars()}
        assert people["gaurav grv"].status == "self" and people["Gaurav Sharma"].status == "active"
        top = await people_context(db, a.id, "Who do I email the most?")
        assert not any(l.startswith("gaurav grv") for l in top)
        assert "look like the user's own other addresses: gaurav grv <gaurav.grv@gmail.com>" in top[-1]
        # The user says it's someone else: it comes back and the guess never hides it again.
        assert (await set_self(db, a.id, "gaurav.grv@gmail.com", False)).status == "active"
        assert await mark_lookalikes(db, a) == 0
        assert any(l.startswith("gaurav grv") for l in await people_context(db, a.id, "Who do I email the most?"))
        # The user says it's theirs after all: hidden, and not reported as a guess.
        await set_self(db, a.id, "gaurav.grv@gmail.com", True)
        assert not any("gaurav.grv" in l for l in await people_context(db, a.id, "Who do I email the most?"))
        # An address never seen before that the user says is theirs is skipped from then on.
        await set_self(db, a.id, "g.home@yahoo.com", True)
        await db.commit()
        assert "g.home@yahoo.com" in await learn.own_addresses(db, a)


async def test_a_colleague_in_sent_items_is_not_taken_for_the_user(factory):
    from app.memory_engine import learn
    from app.memory_engine.entities import _claim_alias, resolve, set_self, unmark_wrong_self
    from app.models import MemoryEntity, User

    a, _ = await _email_user(factory, f"gr{os.getpid()}del@agilitas.test")
    async with factory() as db:
        row = await db.get(User, a.id)
        row.display_name = a.display_name = "Gaurav R"
        await db.commit()
        # Marked by the first Sent-folder rule (sent on Karthik's behalf), never confirmed by the user.
        wrong = await resolve(db, a.id, name="Karthik Yermunja", email="karthik@agilitas.test")
        wrong.status = "self"
        await _claim_alias(db, a.id, wrong.id, "is_me", f"entity:{wrong.id}")
        # One the user did confirm stays theirs, whatever its name.
        mine = await resolve(db, a.id, name="Work Bot", email="bot@agilitas.test")
        await set_self(db, a.id, "bot@agilitas.test", True, by_user=True)
        await db.commit()
        assert await unmark_wrong_self(db, a) == 1
        await db.commit()
        assert (await db.get(MemoryEntity, wrong.id)).status == "active"
        assert (await db.get(MemoryEntity, mine.id)).status == "self"
        box = FakeMailbox(a.email, sent=[
            _msg("d1", frm="Karthik Yermunja <karthik@agilitas.test>", to="Priya Nair <priya@acme.com>"),
        ])
        await learn.learn_mail(db, a, box)
        assert "karthik@agilitas.test" not in await learn.own_addresses(db, a)
