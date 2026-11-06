"""Saved browser runs: plain-English listing and accepting / rejecting / undoing a fix (docs/BROWSING.md
"Healing saved runs")."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent import fast_browser as fb
from app.browser_runs import routes
from app.deps import current_user

USER = SimpleNamespace(id=uuid.uuid4())


def _row(**kw):
    base = dict(
        id=uuid.uuid4(), user_id=USER.id, name="download order status", goal="Download the order status report",
        host="acme.test", start_url="https://acme.test/reports", source="task", skill_id="", params=[],
        steps=[{"kind": "click", "target": {"role": "link", "name": "Reporting"}}],
        uses=4, successes=3, failures=1, proposed_steps=None, proposed_note=None, proposed_at=None, prior_steps=None,
        updated_at=datetime(2026, 11, 6, tzinfo=timezone.utc),
    )  # fmt: skip
    base.update(kw)
    return SimpleNamespace(**base)


def test_describe_trace_shows_steps_and_a_pending_fix():
    row = _row(
        proposed_steps=[{"kind": "click", "target": {"role": "button", "name": "Order Status Report"}}],
        proposed_note=[{"step": 1, "was": 'click the link "Reporting"', "now": ['click the button "Order Status Report"']}],
        proposed_at=datetime(2026, 11, 6, 9, tzinfo=timezone.utc),
    )
    out = fb.describe_trace(row)
    assert out["steps"] == ['click the link "Reporting"']
    assert out["proposed_fix"]["steps"] == ['click the button "Order Status Report"']
    assert out["proposed_fix"]["changes"][0]["step"] == 1 and out["can_undo"] is False
    assert fb.describe_trace(_row())["proposed_fix"] is None


class FakeStore:
    def __init__(self):
        self.calls = []

    async def listing(self, user_id, trace_id=""):
        self.calls.append(("list", user_id, trace_id))
        if trace_id == "missing":
            return []
        return [fb.describe_trace(_row())]

    async def resolve(self, user_id, trace_id, accept):
        self.calls.append(("resolve", trace_id, accept))
        if trace_id == "missing":
            return {"ok": False, "error": "no such saved run"}
        return {"ok": True, "accepted": accept}

    async def undo(self, user_id, trace_id):
        self.calls.append(("undo", trace_id))
        return {"ok": False, "error": "nothing to undo"}

    async def delete(self, user_id, trace_id):
        self.calls.append(("delete", trace_id))
        return trace_id != "missing"


@pytest.fixture
def client(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(routes, "STORE", store)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    app.dependency_overrides[current_user] = lambda: USER
    return SimpleNamespace(http=TestClient(app), store=store)


def test_routes_list_resolve_undo_and_delete(client):
    assert client.http.get("/api/browser/saved-runs").json()[0]["steps"] == ['click the link "Reporting"']
    assert client.http.get("/api/browser/saved-runs/missing").status_code == 404
    assert client.http.post("/api/browser/saved-runs/t1/fix", json={"accept": True}).json() == {"ok": True, "accepted": True}
    assert client.http.post("/api/browser/saved-runs/missing/fix", json={"accept": False}).status_code == 404
    assert client.http.post("/api/browser/saved-runs/t1/undo").status_code == 400
    assert client.http.delete("/api/browser/saved-runs/t1").json() == {"ok": True}
    assert client.http.delete("/api/browser/saved-runs/missing").status_code == 404
    assert ("resolve", "t1", True) in client.store.calls and all(c[1] == USER.id for c in client.store.calls if c[0] == "list")


# --- the store against a real database (set CHECKME_TEST_DATABASE_URL) -------------------------------------------
import os  # noqa: E402

from sqlalchemy import text  # noqa: E402

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")


@pytest.fixture
async def db_store(monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.db as appdb
    from app.models import Base, User

    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(appdb, "SessionLocal", factory)
    uid = uuid.uuid4()
    async with factory() as db:
        db.add(User(id=uid, email=f"{uid}@test.local", primary_provider="google"))
        await db.commit()
    yield fb.TraceStore(), uid
    async with factory() as db:
        await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": uid})
        await db.commit()
    await engine.dispose()


@needs_db
async def test_store_proposes_then_accepts_rejects_and_undoes(db_store):
    store, uid = db_store
    old = [{"kind": "click", "target": {"role": "button", "name": "Old"}}]
    new = [{"kind": "click", "target": {"role": "button", "name": "New"}}, {"kind": "fill", "target": {"label": "From"}, "param": "from_date"}]
    tid = await store.save(uid, host="acme.test", name="report", goal="Get the report", start_url="https://acme.test/", steps=old)
    assert await store.propose(tid, new, [{"step": 1, "was": "click Old", "now": ["click New"]}])
    # Replays keep using the saved steps until the fix is accepted.
    assert (await store.candidates(uid, trace_id=tid))[0]["steps"] == old
    listed = (await store.listing(uid, tid))[0]
    assert listed["proposed_fix"]["steps"][0] == 'click the button "New"'
    # Someone else cannot accept it.
    assert (await store.resolve(uuid.uuid4(), tid, True))["ok"] is False
    out = await store.resolve(uid, tid, True)
    assert out["ok"] and out["accepted"]
    row = (await store.candidates(uid, trace_id=tid))[0]
    assert row["steps"] == new and row["params"] == [{"name": "from_date", "secret": False}]
    assert (await store.listing(uid, tid))[0]["proposed_fix"] is None and (await store.listing(uid, tid))[0]["can_undo"]
    assert (await store.resolve(uid, tid, True))["error"] == "this saved run has no proposed fix"
    # Undo puts the old steps back once.
    assert (await store.undo(uid, tid))["ok"]
    assert (await store.candidates(uid, trace_id=tid))[0]["steps"] == old
    assert (await store.undo(uid, tid))["error"] == "nothing to undo"
    # Reject drops a proposal without touching the steps.
    await store.propose(tid, new, [])
    assert (await store.resolve(uid, tid, False))["accepted"] is False
    assert (await store.candidates(uid, trace_id=tid))[0]["steps"] == old
    assert await store.delete(uid, tid) and await store.listing(uid, tid) == []
