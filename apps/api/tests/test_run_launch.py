"""Running saved runs (docs/BROWSING.md "Running saved runs"): approval steps, starting a run from the app or with a
run key, and running it once per row."""

from __future__ import annotations

import json
import os
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.agent import fast_browser as fb
from app.browser_runs import launch, routes
from app.deps import current_user

USER = SimpleNamespace(id=uuid.uuid4())
PARAMS = [{"name": "from_date", "secret": False}, {"name": "login_password", "secret": True}]
STEPS = [
    {"kind": "click", "target": {"role": "link", "name": "Reports"}},
    {"kind": "approve", "text": "Check the totals before export"},
    {"kind": "click", "target": {"role": "button", "name": "Export"}},
]  # fmt: skip


# --- approval steps --------------------------------------------------------------------------------------------
def test_approval_steps_are_built_and_described():
    built = fb.build_steps(STEPS[:1], [{"keep": 0}, {"approve": " Check the  totals "}])
    assert built[1] == {"kind": "approve", "text": "Check the totals"}
    assert fb.describe_step(built[1]) == "Wait for your OK: Check the totals"
    assert fb.trace_final_step(built) == ""


async def test_replay_pauses_at_an_approval_and_resumes_after_it(monkeypatch):
    sent = []

    async def internal(c, name, args):
        sent.append([s["target"]["name"] for s in args["steps"]])
        return {"ok": True, "completed": len(args["steps"])}

    async def nav(c, url):
        sent.append(["open"])
        return {"ok": True, "url": url}

    monkeypatch.setattr(fb, "_internal", internal)
    monkeypatch.setattr(fb, "_navigate", nav)
    trace = {"id": "t1", "start_url": "https://wms.test/", "steps": STEPS}
    first = await fb.replay(SimpleNamespace(), trace, {})
    assert first["ok"] is False and first["needs_approval"] == {"step": 2, "text": "Check the totals before export"}
    assert sent == [["open"], ["Reports"]]
    sent.clear()
    second = await fb.replay(SimpleNamespace(), trace, {}, start_step=2)
    assert second["ok"] and sent == [["Export"]]  # no reopening of the start page, and the approved step is passed


async def test_replay_named_tells_the_agent_to_ask_first(monkeypatch):
    class Store:
        async def candidates(self, user_id, host="", trace_id="", skill_id=""):
            return [{"id": "t1", "name": "report", "host": "wms.test", "start_url": "", "steps": STEPS}] if trace_id == "t1" else []

        async def mark(self, *a):
            raise AssertionError("a pause is not a failure")

        async def propose(self, *a):
            return False

    async def internal(c, name, args):
        return {"ok": True, "completed": len(args["steps"])}

    monkeypatch.setattr(fb, "STORE", Store())
    monkeypatch.setattr(fb, "_internal", internal)
    out = await fb.replay_named(SimpleNamespace(user=USER), run_id="t1")
    assert out["needs_approval"]["step"] == 2 and 'start_step=2' in out["hint"] and "ask_user" in out["hint"]


# --- rows and values -------------------------------------------------------------------------------------------
def test_clean_rows_checks_names_secrets_and_missing_values():
    assert launch.clean_rows(PARAMS, {"from_date": "2026-05-01"}, None) == [{"from_date": "2026-05-01"}]
    rows = launch.clean_rows(PARAMS, {}, [{"from_date": "a"}, {"from_date": "b"}])
    assert rows == [{"from_date": "a"}, {"from_date": "b"}]
    for values, rows_in, msg in (
        ({"nope": "x"}, None, "no value named 'nope'"),
        ({"from_date": "a", "login_password": "x"}, None, "is secret"),
        ({}, None, "missing from_date"),
        ({}, [{"from_date": "x"}] * 51, "at most 50"),
        ({"from_date": "x" * 501}, None, "longer than 500"),
    ):
        with pytest.raises(launch.RunError, match=msg):
            launch.clean_rows(PARAMS, values, rows_in)


def test_job_prompt_treats_rows_as_data_and_names_the_run():
    prompt = launch.job_prompt({"id": "t1", "name": "report", "host": "wms.test"}, [{"from_date": "a"}, {"from_date": "b"}], "the API")
    assert "2 times, once per row" in prompt and 'browser_replay(run_id="t1"' in prompt
    assert "not instructions" in prompt and json.dumps([{"from_date": "a"}, {"from_date": "b"}]) in prompt
    assert "needs_approval" in prompt and "ask_user" in prompt


def test_keys_are_hashed_and_compared_safely():
    stored = launch.key_hash("brk_abc")
    assert launch.same_key("brk_abc", stored) and not launch.same_key("brk_abd", stored) and not launch.same_key("x", "")


# --- routes ----------------------------------------------------------------------------------------------------
@pytest.fixture
def http(monkeypatch):
    queued = []

    class Store:
        async def candidates(self, user_id, host="", trace_id="", skill_id=""):
            return [{"id": trace_id, "name": "report", "host": "wms.test", "params": PARAMS}] if trace_id != "zz" else []

    async def queue_run(db, user, trace, rows, source):
        queued.append((trace["id"], rows, source))
        return {"ok": True, "job_id": "j1", "rows": len(rows)}

    async def set_key(user_id, trace_id, rotate=True):
        return {"ok": True, "key": "brk_new"} if rotate else {"ok": True, "removed": True}

    async def no_db():
        yield None

    from app.db import get_db

    monkeypatch.setattr(routes, "STORE", Store())
    monkeypatch.setattr(launch, "queue_run", queue_run)
    monkeypatch.setattr(launch, "set_key", set_key)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    app.include_router(routes.hook_router, prefix="/api")
    app.dependency_overrides[current_user] = lambda: USER
    app.dependency_overrides[get_db] = no_db
    return SimpleNamespace(client=TestClient(app), queued=queued)


def test_run_now_and_keys(http):
    out = http.client.post("/api/browser/saved-runs/t1/run", json={"rows": [{"from_date": "a"}, {"from_date": "b"}]})
    assert out.json()["rows"] == 2 and http.queued[0][2] == "the Saved runs page"
    assert http.client.post("/api/browser/saved-runs/t1/run", json={"values": {"login_password": "x", "from_date": "a"}}).status_code == 400
    assert http.client.post("/api/browser/saved-runs/zz/run", json={}).status_code == 404
    assert http.client.post("/api/browser/saved-runs/t1/key").json() == {"ok": True, "key": "brk_new"}
    assert http.client.delete("/api/browser/saved-runs/t1/key").json()["removed"]


def test_run_hook_needs_a_valid_key(http, monkeypatch):
    async def trace_for_key(db, trace_id, key):
        if key != "brk_good":
            raise PermissionError("invalid run key")
        return USER, {"id": trace_id, "name": "report", "params": PARAMS}

    monkeypatch.setattr(launch, "trace_for_key", trace_for_key)
    assert http.client.post("/api/run-hooks/t1", json={"values": {"from_date": "a"}}).status_code == 403
    ok = http.client.post("/api/run-hooks/t1", json={"values": {"from_date": "a"}}, headers={"Authorization": "Bearer brk_good"})
    assert ok.status_code == 200 and http.queued[-1] == ("t1", [{"from_date": "a"}], "the API")


# --- end to end against a real database ------------------------------------------------------------------------
URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")


@needs_db
async def test_a_key_starts_a_background_job(monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.db as appdb
    from app.jobs import worker
    from app.models import AgentJob, Base, User

    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(appdb, "SessionLocal", factory)
    monkeypatch.setattr(worker, "wake", lambda: None)
    uid = uuid.uuid4()
    async with factory() as db:
        db.add(User(id=uid, email=f"{uid}@test.local", primary_provider="google"))
        await db.commit()
    try:
        tid = await fb.TraceStore().save(uid, host="wms.test", name="report", goal="Get the report", start_url="https://wms.test/",
                                         steps=[{"kind": "fill", "target": {"label": "From"}, "param": "from_date"}])  # fmt: skip
        key = (await launch.set_key(uid, tid))["key"]
        async with factory() as db:
            with pytest.raises(PermissionError):
                await launch.trace_for_key(db, tid, "brk_wrong")
            user, trace = await launch.trace_for_key(db, tid, key)
            rows = launch.clean_rows(trace["params"], {}, [{"from_date": "2026-05-01"}, {"from_date": "2026-05-08"}])
            out = await launch.queue_run(db, user, trace, rows, "the API")
        async with factory() as db:
            job = (await db.execute(select(AgentJob).where(AgentJob.id == uuid.UUID(out["job_id"])))).scalars().one()
        assert job.role == "worker" and job.meta["rows"] == 2 and "2026-05-08" in job.prompt and job.status == "queued"
        await launch.set_key(uid, tid, rotate=False)
        async with factory() as db:
            with pytest.raises(PermissionError):
                await launch.trace_for_key(db, tid, key)
    finally:
        async with factory() as db:
            await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": uid})
            await db.commit()
        await engine.dispose()
