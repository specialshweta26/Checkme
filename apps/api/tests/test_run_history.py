"""Run history (docs/BROWSING.md "Run history"): each browser_task run is kept step by step with reasons and
screenshots; healing and AI steps add to the run they are part of."""

from __future__ import annotations

import io
import os
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.agent import fast_browser as fb
from app.browser_runs import routes, runlog
from app.config import get_settings
from app.deps import current_user

USER = SimpleNamespace(id=uuid.uuid4())


def _png() -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (1440, 900), (40, 90, 200)).save(out, "PNG")
    return out.getvalue()


def test_thumbnail_is_a_small_jpeg():
    jpg = runlog.thumbnail(_png())
    from PIL import Image

    img = Image.open(io.BytesIO(jpg))
    assert img.format == "JPEG" and img.width == runlog.SHOT_WIDTH
    assert runlog.thumbnail(b"not an image") == b""


@pytest.mark.parametrize(
    "out,status",
    [({"ok": True, "done": True, "via": "model"}, "completed"), ({"ok": True, "done": True, "via": "replay"}, "replayed"),
     ({"ok": True, "done": True, "healed": [1]}, "recovered"), ({"ok": False, "need_user": "code"}, "needs_you"),
     ({"ok": True, "stopped_before": "Pay"}, "needs_you"), ({"ok": False, "fallback": True}, "stopped")],
)  # fmt: skip
def test_status_for(out, status):
    assert runlog.status_for(out) == status


def test_summarize_actions_names_targets_and_hides_values():
    state = {"elements": [{"ref": "e1", "name": "Email"}, {"ref": "e2", "name": "Sign in"}]}
    acts = [{"kind": "fill", "ref": "e1", "text": "{{login_username}}"}, {"kind": "fill", "ref": "e1", "text": "ops@x.test"},
            {"kind": "click", "ref": "e2"}]  # fmt: skip
    line = fb.summarize_actions(acts, state, {"email": "ops@x.test"})
    assert line == "fill \"Email\" with {{login_username}}, fill \"Email\" with '•••', click \"Sign in\""


async def test_a_run_is_logged_with_reasons_and_nested_runs_join_it(monkeypatch):
    monkeypatch.setattr(get_settings(), "browser_run_log", True)
    finished = []

    async def finish(self, out, trace_id=""):
        import asyncio

        await asyncio.gather(*self._pending)
        finished.append((self, out, trace_id))
        self._saved = True

    async def shot(self, row):
        row["shot"] = "k.jpg"

    monkeypatch.setattr(runlog.RunLog, "finish", finish)
    monkeypatch.setattr(runlog.RunLog, "_shot", shot)
    plans = [
        {"why": "The sign-in form is open.", "actions": [{"kind": "click", "ref": "e3"}], "done": False, "result": "", "need_user": ""},
        {"why": "Signed in.", "actions": [], "done": True, "result": "Home page", "need_user": ""},
    ]  # fmt: skip

    async def plan_step(*a, **k):
        return fb.parse_plan(__import__("json").dumps(plans.pop(0)))

    async def act(c, actions):
        return {"ok": True, "url": "https://acme.test/home", "title": "Home", "text": "", "tree": "", "completed": 1,
                "trace": [{"i": 0, "target": {"role": "button", "name": "Sign in"}}]}  # fmt: skip

    async def nav(c, url):
        return {"ok": True, "url": url, "title": "Sign in", "text": "", "tree": "", "elements": [{"ref": "e3", "name": "Sign in"}]}

    class Store:
        async def candidates(self, *a, **k):
            return []

        async def save(self, *a, **k):
            return "trace-1"

    monkeypatch.setattr(fb, "plan_step", plan_step)
    monkeypatch.setattr(fb, "_act", act)
    monkeypatch.setattr(fb, "_navigate", nav)
    monkeypatch.setattr(fb, "STORE", Store())
    from app.browser_runs import logins

    async def none(*a):
        return {}

    monkeypatch.setattr(logins, "values_for", none)
    out = await fb.browser_task(SimpleNamespace(user=USER), "Sign in to Acme", "https://acme.test/login", {})
    assert out["done"] and out["run_id"]
    run, result, trace_id = finished[0]
    assert trace_id == "trace-1" and "_trace_id" not in out and run.host == "acme.test"
    assert run.steps[0]["action"] == 'click "Sign in"' and run.steps[0]["why"] == "The sign-in form is open."
    assert run.steps[0]["ok"] and run.steps[0]["shot"] == "k.jpg" and run.steps[0]["url"] == "https://acme.test/home"
    # A browser_task inside a run (healing, an AI step) adds to it instead of starting its own.
    parent = runlog.RunLog(USER.id, "parent")
    token = runlog.CURRENT.set(parent)
    try:
        plans.extend([{"why": "x", "actions": [], "done": True, "result": "ok", "need_user": ""}])
        await fb.browser_task(SimpleNamespace(user=USER), "inner", "https://acme.test/login", {}, use_saved=False, save=False)
    finally:
        runlog.CURRENT.reset(token)
    assert len(finished) == 1


def test_routes_list_and_show_runs(monkeypatch):
    async def list_runs(user_id, trace_id="", limit=50):
        return [{"id": "r1", "goal": "g", "status": "completed", "trace_id": trace_id or None}]

    async def get_run(user_id, run_id):
        return None if run_id == "zz" else {"id": run_id, "step_rows": [{"i": 1, "action": "click", "shot_url": "u"}]}

    monkeypatch.setattr(runlog, "list_runs", list_runs)
    monkeypatch.setattr(runlog, "get_run", get_run)
    app = FastAPI()
    app.include_router(routes.runs_router, prefix="/api")
    app.dependency_overrides[current_user] = lambda: USER
    http = TestClient(app)
    assert http.get("/api/browser/runs?trace_id=t9").json()[0]["trace_id"] == "t9"
    assert http.get("/api/browser/runs/r1").json()["step_rows"][0]["shot_url"] == "u"
    assert http.get("/api/browser/runs/zz").status_code == 404


URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")


@needs_db
async def test_runs_are_saved_listed_scoped_and_pruned(monkeypatch, tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.db as appdb
    from app.models import Base, User

    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(appdb, "SessionLocal", factory)
    monkeypatch.setattr(get_settings(), "file_storage", "disk")
    monkeypatch.setattr(get_settings(), "storage_disk_path", str(tmp_path))
    from app.connectors import daytona_box as box

    uid = uuid.uuid4()
    box.remember_screen(_png(), str(uid))
    async with factory() as db:
        db.add(User(id=uid, email=f"{uid}@test.local", primary_provider="google"))
        await db.commit()
    try:
        run = runlog.RunLog(uid, "Download the report", "acme.test")
        await run.step('click "Reports"', "The menu holds the report.", True, {"url": "https://acme.test/r", "title": "Reports"})
        await run.finish({"ok": True, "done": True, "via": "model", "result": "Downloaded"}, "trace-1")
        listed = await runlog.list_runs(uid)
        assert listed[0]["status"] == "completed" and listed[0]["trace_id"] == "trace-1" and listed[0]["steps"] == 1
        shown = await runlog.get_run(uid, listed[0]["id"])
        assert shown["step_rows"][0]["why"] == "The menu holds the report." and shown["step_rows"][0]["shot_url"]
        assert await runlog.get_run(uuid.uuid4(), listed[0]["id"]) is None
        assert await runlog.list_runs(uid, trace_id="other") == []
        for n in range(3):
            extra = runlog.RunLog(uid, f"run {n}")
            await extra.finish({"ok": False})
        assert await runlog.prune(uid, keep=2) == 2 and len(await runlog.list_runs(uid)) == 2
    finally:
        async with factory() as db:
            await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": uid})
            await db.commit()
        await engine.dispose()
