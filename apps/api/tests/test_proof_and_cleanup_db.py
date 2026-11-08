"""Proof numbers and storage tidying, against a real Postgres: how much each sent reply differed from the draft (per
week), triage tier counts per user, and browser-run screenshots older than 30 days removed except each saved run's
newest run, plus files whose run is gone.

Skipped unless CHECKME_TEST_DATABASE_URL is set (see tests/test_user_memory_db.py).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI

from tests.test_user_memory_db import _user_with_agents, factory  # noqa: F401 - the fixture is used by name

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")
NOW = datetime.now(timezone.utc)


def test_draft_similarity():
    from app.inbox.service import draft_similarity

    draft = "Hi Priya,\n\nThanks for the update. The SOW will be with you by Friday.\n\nBest, Gaurav"
    assert draft_similarity(draft, draft.replace("\n\n", "\n")) == 1.0  # whitespace doesn't count
    assert 0.7 <= draft_similarity(draft, draft.replace("Thanks for the update.", "Thanks!")) < 0.97
    assert draft_similarity(draft, "Friday. G") < 0.7
    assert draft_similarity("", "anything") is None and draft_similarity("No reply is needed to this message.", "x") is None


@pytest.fixture(autouse=True)
def clean_meter():
    from app.agent import meter

    meter._pending.clear()
    yield
    meter._pending.clear()


async def test_usage_shows_draft_edits_by_week_and_triage_tiers(factory, monkeypatch):
    from app.agent import meter
    from app.auth.routes import me_router
    from app.db import get_db
    from app.deps import current_user
    from app.models import InboxItem

    a, _ = await _user_with_agents(factory)
    async with factory() as db:
        for sim, days in ((1.0, 1), (0.85, 1), (0.4, 1), (0.99, 9)):
            db.add(InboxItem(user_id=a.id, connector_id=uuid.uuid4(), message_id=f"m-{uuid.uuid4().hex[:8]}", status="sent",
                             sent_similarity=sim, sent_at=NOW - timedelta(days=days)))
        await db.commit()
    with meter.metered(a.id, "inbox"):
        for tier in ("rule", "rule", "rule", "small", "full"):
            meter.count(f"triage:{tier}")
    app = FastAPI()
    app.include_router(me_router, prefix="/api")

    async def db_dep():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db] = db_dep
    app.dependency_overrides[current_user] = lambda: a
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app.test") as http:
        data = (await http.get("/api/me/usage")).json()
    assert data["triage"] == {"rule": 3, "small": 1, "full": 1}
    assert "inbox" not in data["by_feature"] or data["by_feature"]["inbox"]["calls"] == 0  # tiers aren't model calls
    totals = {k: sum(w[k] for w in data["drafts"]) for k in ("unchanged", "light", "rewritten")}
    assert totals == {"unchanged": 2, "light": 1, "rewritten": 1} and len(data["drafts"]) >= 1


async def test_old_screenshots_go_except_each_saved_runs_newest(factory, monkeypatch):
    from app import storage
    from app.browser_runs.runlog import tidy_screenshots
    from app.models import BrowserRun, BrowserTrace

    a, _ = await _user_with_agents(factory)
    deleted: list[str] = []
    files: list[str] = []
    monkeypatch.setattr(storage, "list_keys", lambda prefix: [k for k in files if k.startswith(prefix)])
    monkeypatch.setattr(storage, "delete_keys", lambda keys: deleted.extend(keys))

    async with factory() as db:
        trace = BrowserTrace(user_id=a.id, host="shop.test", goal="Reorder", name="Reorder coffee")
        db.add(trace)
        await db.flush()

        def run(days, trace_id=""):
            r = BrowserRun(id=uuid.uuid4(), user_id=a.id, trace_id=trace_id, goal="g", status="completed",
                           started_at=NOW - timedelta(days=days))
            r.steps = [{"i": i, "action": "click", "shot": f"{a.id}/browser-runs/{r.id}/{i}.jpg"} for i in range(2)]
            files.extend(s["shot"] for s in r.steps)
            db.add(r)
            return r

        old_plain, recent = run(40), run(5)
        old_saved_older, old_saved_newest = run(60, str(trace.id)), run(45, str(trace.id))
        await db.commit()
    files.append(f"{a.id}/browser-runs/{uuid.uuid4()}/0.jpg")  # a leftover whose run was deleted
    done = await tidy_screenshots(factory, a.id)
    assert done == {"old": 4, "orphans": 1}
    async with factory() as db:
        got = {r.id: r for r in (await db.execute(BrowserRun.__table__.select().where(BrowserRun.user_id == a.id))).all()}
        steps = {rid: [s.get("shot") for s in row.steps] for rid, row in got.items()}
    assert steps[old_plain.id] == [None, None] and steps[old_saved_older.id] == [None, None]  # pictures gone, steps kept
    assert all(steps[recent.id]) and all(steps[old_saved_newest.id])  # recent, and the saved run's newest, untouched
    assert len(deleted) == 5 and not [k for k in deleted if str(recent.id) in k or str(old_saved_newest.id) in k]
