"""Watches (docs/WATCHES.md): agents wake when a page changes or the inbox fills up, not only on a clock."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.automations import watches

PAGE_V1 = "Acme Pricing\nStarter $19 per month\nPro $49 per month\nContact sales"
PAGE_V2 = "Acme Pricing\nStarter $19 per month\nPro $59 per month\nContact sales"
PAGE_V3 = "Acme Pricing\nStarter $19 per month\nPro $59 per month\nEnterprise plan now available\nContact sales"


def test_first_copy_is_the_baseline_then_changes_fire():
    fire, state, _ = watches.page_change({}, PAGE_V1)
    assert fire is False and state["hash"]
    fire, same, _ = watches.page_change(state, PAGE_V1 + "\n\n   ")  # whitespace only
    assert fire is False
    fire, state2, change = watches.page_change(state, PAGE_V2)
    assert fire is True and change == {"added": ["Pro $59 per month"], "removed": ["Pro $49 per month"]}


def test_a_phrase_fires_only_when_it_newly_appears():
    _, s1, _ = watches.page_change({}, PAGE_V1, "Enterprise")
    fire, s2, _ = watches.page_change(s1, PAGE_V2, "Enterprise")
    assert fire is False  # changed, but not the phrase
    fire, s3, change = watches.page_change(s2, PAGE_V3, "enterprise")
    assert fire is True and "Enterprise plan now available" in change["added"]
    fire, _, _ = watches.page_change(s3, PAGE_V3 + "\nNew footer", "Enterprise")
    assert fire is False  # still there: no repeat


def test_inbox_watch_fires_once_per_crossing():
    fire, s = watches.inbox_crossing({}, 12, 20)
    assert fire is False
    fire, s = watches.inbox_crossing(s, 25, 20)
    assert fire is True
    fire, s = watches.inbox_crossing(s, 30, 20)
    assert fire is False  # still above: already told
    fire, s = watches.inbox_crossing(s, 18, 20)
    fire, s = watches.inbox_crossing(s, 21, 20)
    assert fire is True  # re-armed after dropping back


def test_settings_are_bounded():
    assert watches.every(1) == 15 and watches.every(10_000) == 1440 and watches.every("x") == 60
    assert watches.threshold("20") == 20
    with pytest.raises(ValueError):
        watches.threshold("lots")
    assert watches.clean_url("acme.com/pricing") == "https://acme.com/pricing"
    with pytest.raises(ValueError):
        watches.clean_url("not a url")


class FakeDB:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


def _row(**kw):
    base = dict(id=7, user_id=uuid.uuid4(), agent_id=uuid.uuid4(), name="Acme pricing", trigger="https://acme.example/pricing",
                condition="", action="Tell me what changed and whether we should match it.", every_minutes=60, state={}, enabled=True)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def queued(monkeypatch):
    calls = []

    async def queue_event(db, row, event_id, event_name, prompt, **kw):
        calls.append({"event_id": event_id, "name": event_name, "prompt": prompt})
        return {"queued": True}

    monkeypatch.setattr(watches.service, "queue_event", queue_event)
    return calls


async def test_check_page_records_then_wakes_the_agent_with_the_change(queued):
    pages = iter([{"ok": True, "text": PAGE_V1, "title": "Pricing"}, {"ok": True, "text": PAGE_V2, "title": "Pricing"}])

    async def fetch(url):
        return next(pages)

    row, db = _row(), FakeDB()
    first = await watches.check_page(db, row, fetch=fetch)
    assert queued == [] and first["checks"] == 1 and first["hash"]
    second = await watches.check_page(db, row, fetch=fetch)
    assert len(queued) == 1 and second["fired"] == 1 and second["last_change"]["added"] == ["Pro $59 per month"]
    prompt = queued[0]["prompt"]
    assert "Page watch: Acme pricing" in prompt and "the page changed" in prompt
    assert "untrusted data" in prompt and "Pro $59 per month" in prompt  # page text is wrapped as untrusted
    assert queued[0]["event_id"].startswith("page:")


async def test_blocked_or_failing_pages_report_an_error_and_never_fire(queued):
    async def blocked(url):
        return {"ok": False, "block": "captcha", "text": ""}

    async def boom(url):
        raise RuntimeError("timed out")

    row, db = _row(state={"hash": "abc", "lines": ["x"]}), FakeDB()
    s = await watches.check_page(db, row, fetch=blocked)
    assert "block" in s["error"] and s["hash"] == "abc"
    s = await watches.check_page(db, row, fetch=boom)
    assert s["error"] == "timed out" and queued == []


async def test_due_respects_the_interval():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    assert watches._due(_row(), now)
    assert not watches._due(_row(state={"checked_at": (now - timedelta(minutes=10)).isoformat()}), now)
    assert watches._due(_row(state={"checked_at": (now - timedelta(minutes=61)).isoformat()}), now)


async def test_private_addresses_cannot_be_watched():
    from app.automations.triggers import create_trigger

    user = SimpleNamespace(id=uuid.uuid4())
    for bad in ("http://127.0.0.1/admin", "https://127.0.0.1/admin", "https://169.254.169.254/latest/meta-data", "https://10.0.0.5", "https://localhost/"):
        with pytest.raises(ValueError, match="can't be watched"):
            await create_trigger(None, user, trigger_type="page", name="x", trigger=bad, action="y")
