"""Cards in Chief's window, against a real Postgres: listed per user, strongest reason first; Not now removes a card;
a sender dismissed three times stops getting key-contact cards (urgent and promise ones still come); sending from a
card marks it answered.

Skipped unless CHECKME_TEST_DATABASE_URL is set (see tests/test_user_memory_db.py).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI

from tests.test_user_memory_db import _user_with_agents, factory  # noqa: F401 - the fixture is used by name

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")


def _client(factory, user, monkeypatch):
    from app.db import get_db
    from app.deps import current_user
    from app.inbox import service
    from app.inbox.routes import router

    async def no_connectors(db, u):
        return []

    monkeypatch.setattr(service, "mail_connectors", no_connectors)
    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def db_dep():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db] = db_dep
    app.dependency_overrides[current_user] = lambda: user
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app.test")


async def _card(factory, user, sender, reason, state="new"):
    from app.models import InboxItem

    async with factory() as db:
        item = InboxItem(user_id=user.id, connector_id=uuid.uuid4(), message_id=f"m-{uuid.uuid4().hex[:8]}", sender=sender,
                         subject=f"{reason} from {sender}", classification="reply", summary="s", draft="d",
                         alert_reason=reason, alert_state=state, alerted_at=datetime.now(timezone.utc))
        db.add(item)
        await db.commit()
        return item.id


async def test_cards_per_user_strongest_first_and_not_now(factory, monkeypatch):
    a, _ = await _user_with_agents(factory)
    b, _ = await _user_with_agents(factory)
    key = await _card(factory, a, "Karthik <karthik@agilitas.test>", "key_contact")
    promise = await _card(factory, a, "Priya <priya@acme.test>", "promise")
    await _card(factory, b, "Someone <x@y.test>", "urgent")
    async with _client(factory, a, monkeypatch) as http:
        cards = (await http.get("/api/inbox/alerts")).json()["alerts"]
        assert [c["id"] for c in cards] == [str(promise), str(key)]  # only a's, promise first
        assert cards[0]["alert_label"] == "About something you promised"
        assert (await http.post(f"/api/inbox/{key}/alert", json={"state": "dismissed"})).json() == {"ok": True}
        assert [c["id"] for c in (await http.get("/api/inbox/alerts")).json()["alerts"]] == [str(promise)]


async def test_a_sender_you_keep_dismissing_stops_getting_key_contact_cards(factory, monkeypatch):
    a, _ = await _user_with_agents(factory)
    sender = "Newsy Colleague <newsy@agilitas.test>"
    for _ in range(3):
        await _card(factory, a, sender, "key_contact", state="dismissed")
    key = await _card(factory, a, sender, "key_contact")
    urgent = await _card(factory, a, sender, "urgent")
    async with _client(factory, a, monkeypatch) as http:
        ids = [c["id"] for c in (await http.get("/api/inbox/alerts")).json()["alerts"]]
    assert str(urgent) in ids and str(key) not in ids  # muted for key-contact cards, not for urgent ones
    # Answering one from a card un-mutes the sender.
    await _card(factory, a, sender, "key_contact", state="sent")
    async with _client(factory, a, monkeypatch) as http:
        assert str(key) in [c["id"] for c in (await http.get("/api/inbox/alerts")).json()["alerts"]]
