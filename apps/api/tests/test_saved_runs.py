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

    async def edit(self, user_id, trace_id, items, name=""):
        self.calls.append(("edit", trace_id, items, name))
        if trace_id == "missing":
            return {"ok": False, "error": "no such saved run"}
        try:
            fb.build_steps([{"kind": "click"}], items)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "run": fb.describe_trace(_row())}

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
    edited = client.http.put("/api/browser/saved-runs/t1/steps", json={"steps": [{"keep": 0}, {"goal": "Pick last month"}], "name": "Monthly"})
    assert edited.status_code == 200 and edited.json()["step_rows"][0]["text"] == 'click the link "Reporting"'
    assert client.http.put("/api/browser/saved-runs/t1/steps", json={"steps": [{"keep": 5}]}).status_code == 400
    assert client.http.put("/api/browser/saved-runs/missing/steps", json={"steps": [{"keep": 0}]}).status_code == 404
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


# --- editing: exact steps and AI steps ---------------------------------------------------------------------------
OLD = [
    {"kind": "click", "target": {"role": "link", "name": "Reporting"}},
    {"kind": "fill", "target": {"label": "From"}, "param": "from_date"},
    {"kind": "click", "target": {"role": "button", "name": "Apply"}},
]  # fmt: skip


def test_build_steps_keeps_reorders_and_adds_ai_steps():
    new = fb.build_steps(OLD, [{"keep": 0}, {"goal": "  Set the date range to {{from_date}}  to {{to_date}} "}, {"keep": 2}])
    assert new[0] == OLD[0] and new[2] == OLD[2]
    assert new[1] == {"kind": "goal", "text": "Set the date range to {{from_date}} to {{to_date}}"}
    assert fb.trace_params(new) == [{"name": "from_date", "secret": False}, {"name": "to_date", "secret": False}]
    for bad, msg in (([], "at least one"), ([{"keep": 7}], "no saved step 8"), ([{"goal": " "}], "write what"), ([{"x": 1}], "keep, goal, approve or check")):
        with pytest.raises(ValueError, match=msg):
            fb.build_steps(OLD, bad)


def test_ai_steps_read_as_written_and_count_as_final_when_they_say_so():
    assert fb.describe_step({"kind": "goal", "text": "Pick last month in the date filter"}) == "Pick last month in the date filter"
    assert fb.trace_final_step([{"kind": "goal", "text": "Then click Send to the team"}]).lower() == "send"
    assert fb.trace_final_step([{"kind": "goal", "text": "Open the {{send_to}} report"}]) == ""


def test_replay_steps_keeps_ai_step_placeholders_and_reports_missing():
    steps, missing = fb.replay_steps([{"kind": "goal", "text": "Use {{from_date}} and {{to_date}}"}], {"from_date": "x"})
    assert steps[0]["text"] == "Use {{from_date}} and {{to_date}}" and missing == ["to_date"]


async def test_replay_runs_ai_steps_with_the_fast_loop(monkeypatch):
    trace = {"id": "t1", "goal": "Get the report", "start_url": "",
             "steps": [OLD[0], {"kind": "goal", "text": "Set From to {{from_date}}"}, OLD[2]]}  # fmt: skip
    sent, goals = [], []

    async def internal(c, name, args):
        sent.append([s.get("target", {}).get("name") for s in args["steps"]])
        return {"ok": True, "completed": len(args["steps"])}

    async def task(c, goal, url="", values=None, max_steps=0, allow_final_action=False, use_saved=True, save=True):
        goals.append((goal, dict(values or {}), save, use_saved))
        return {"ok": True, "done": True, "page": {"url": "https://acme.test/r"}}

    async def snap(c, find=""):
        return {}

    monkeypatch.setattr(fb, "_internal", internal)
    monkeypatch.setattr(fb, "browser_task", task)
    monkeypatch.setattr(fb, "_snapshot", snap)
    out = await fb.replay(SimpleNamespace(), trace, {"from_date": "2026-05-01"})
    assert out["ok"] and out["completed"] == 3
    assert sent == [["Reporting"], ["Apply"]]  # exact steps around the AI step
    assert "Set From to {{from_date}}" in goals[0][0] and goals[0][1] == {"from_date": "2026-05-01"}
    assert goals[0][2] is False and goals[0][3] is False


async def test_replay_stops_when_an_ai_step_fails(monkeypatch):
    trace = {"id": "t1", "goal": "g", "start_url": "", "steps": [{"kind": "goal", "text": "Open the Order report"}, OLD[2]]}

    async def task(*a, **k):
        return {"ok": False, "done": False, "fallback": True, "note": "could not find it", "page": {}}

    async def internal(c, name, args):
        raise AssertionError("must stop before later steps")

    monkeypatch.setattr(fb, "browser_task", task)
    monkeypatch.setattr(fb, "_internal", internal)
    out = await fb.replay(SimpleNamespace(), trace, {})
    assert out["ok"] is False and out["ai_step"] == 1 and "could not find it" in out["error"]


@needs_db
async def test_store_edit_rewrites_steps_and_drops_a_stale_fix(db_store):
    store, uid = db_store
    tid = await store.save(uid, host="acme.test", name="report", goal="Get the report", start_url="https://acme.test/", steps=OLD)
    await store.propose(tid, OLD[:1], [{"step": 2}])
    out = await store.edit(uid, tid, [{"keep": 0}, {"goal": "Choose last month"}, {"keep": 2}], name="Monthly report")
    assert out["ok"] and out["run"]["name"] == "Monthly report" and out["run"]["proposed_fix"] is None
    assert [r["ai"] for r in out["run"]["step_rows"]] == [False, True, False]
    assert (await store.undo(uid, tid))["ok"] and (await store.candidates(uid, trace_id=tid))[0]["steps"] == OLD
    assert (await store.edit(uid, tid, [{"keep": 9}]))["error"] == "step 1: there is no saved step 10"
    assert (await store.edit(uuid.uuid4(), tid, [{"keep": 0}]))["error"] == "no such saved run"
