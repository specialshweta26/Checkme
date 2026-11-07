"""Inbox triage uses the cheapest tier that does the job: rules for automated mail, the small model to sort the rest,
and the main model only when a reply is needed or the sender is someone the user knows."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.inbox import service
from app.inbox.memory_context import MailContext


def _item(sender, subject="Hello", snippet="Preview text"):
    return SimpleNamespace(id=uuid.uuid4(), sender=sender, cc_header="", subject=subject, message_id="m1", thread_id="t1",
                           received_at="Nov 07, 2026", snippet=snippet, to_header="", recipient_role="to", classification="",
                           summary="", key_points=[], draft="", ai_draft="", key_person=False, sender_note="")


class Box:
    def __init__(self):
        self.calls = []

    async def call(self, tool, args):
        self.calls.append(tool)
        return {"subject": "Hello", "messages": [{"from": "x", "date": "today", "body": "Body " * 2000}]}


@pytest.fixture
def llm(monkeypatch):
    calls = []
    replies = {}

    async def fake(prompt, max_tokens, json_mode=False, model=None):
        tier = "small" if model else "main"
        calls.append((tier, len(prompt)))
        return json.dumps(replies.get(tier, {"classification": "reply", "summary": "S", "key_points": ["k"], "draft": "Hi, thanks. Gaurav"}))

    monkeypatch.setattr(service, "_llm", fake)
    monkeypatch.setattr("app.llm.small_model", lambda: "nano")
    return SimpleNamespace(calls=calls, replies=replies)


async def test_automated_mail_needs_no_model_and_no_thread_fetch(llm):
    item, box = _item("Sophos <do-not-reply@cloud.sophos.com>", "Messages quarantined", "1 message held"), Box()
    await service._classify_one(item, box, MailContext())
    assert llm.calls == [] and box.calls == []
    assert item.classification == "discard" and item.summary == "Automated message from Sophos: Messages quarantined."
    assert item.key_points == ["1 message held"] and item.draft == item.ai_draft == "No reply is needed to this message."


async def test_the_small_model_settles_mail_that_needs_nothing(llm):
    llm.replies["small"] = {"classification": "discard", "summary": "A webinar invite.", "key_points": ["Webinar on Friday"]}
    item = _item("Event Team <events@vendor.test>")
    await service._classify_one(item, Box(), MailContext())
    assert [t for t, _ in llm.calls] == ["small"]
    assert llm.calls[0][1] < 16_000  # the small model reads the newest messages, clipped
    assert item.classification == "discard" and item.summary == "A webinar invite." and item.draft.startswith("No reply")


async def test_the_main_model_drafts_only_when_a_reply_is_needed(llm):
    llm.replies["small"] = {"classification": "reply", "summary": "s", "key_points": []}
    item = _item("Romit <romit@sapient.test>")
    await service._classify_one(item, Box(), MailContext())
    assert [t for t, _ in llm.calls] == ["small", "main"] and item.draft == "Hi, thanks. Gaurav"


async def test_known_people_and_redraft_go_straight_to_the_main_model(llm):
    known = _item("Priya Nair <priya@acme.com>")
    await service._classify_one(known, Box(), MailContext(people=["Priya Nair: Head of Ops"], key_person=True))
    assert [t for t, _ in llm.calls] == ["main"] and known.key_person is True
    llm.calls.clear()
    # Redraft is the user asking for the best draft: no shortcuts, even for an automated sender.
    alert = _item("Alerts <alerts@bank.test>")
    await service._classify_one(alert, Box(), MailContext(), full=True)
    assert [t for t, _ in llm.calls] == ["main"]


async def test_an_automated_address_memory_knows_as_a_person_is_still_read(llm):
    llm.replies["small"] = {"classification": "respond", "summary": "s", "key_points": []}
    item = _item("Support Desk <notifications@partner.test>")
    await service._classify_one(item, Box(), MailContext(people=["Support Desk: our partner's support team"]))
    assert [t for t, _ in llm.calls] == ["main"]
