"""Run traces for every job (docs/RUN_TRACES.md): each step's tool, reasoning, inputs, result and screenshot."""

from __future__ import annotations

import io
import json
import os
import uuid
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import text

from app.jobs import trace

# ---------------------------------------------------------------- no database


def test_inputs_hide_secrets_and_stay_short():
    args = {
        "url": "https://example.com/login",
        "password": "hunter2",
        "headers": {"Authorization": "Bearer abc", "Accept": "json"},
        "note": "key sk-abcdefghijklmnopqrstuv and more",
        "text": "x" * 5000,
        "items": list(range(100)),
    }
    out = trace.safe_args(args)
    assert out["url"] == "https://example.com/login"
    assert out["password"] == trace.HIDDEN and out["headers"]["Authorization"] == trace.HIDDEN
    assert out["headers"]["Accept"] == "json"
    assert "sk-abc" not in json.dumps(out) and len(out["text"]) <= trace.ARG_CHARS
    assert len(json.dumps(out)) <= trace.ARGS_CHARS + 200


def test_reasoning_takes_thinking_and_text():
    msg = AIMessage(
        content=[{"type": "thinking", "thinking": "The user wants Friday's invoices."}, {"type": "text", "text": "I'll search the inbox."}],
        tool_calls=[{"name": "search_mail", "args": {}, "id": "1"}],
    )
    assert trace.reasoning(msg) == "The user wants Friday's invoices. I'll search the inbox."
    assert trace.reasoning(AIMessage(content="")) == ""


def test_outcome_reads_tool_results():
    assert trace.outcome(ToolMessage(content='{"ok": true, "count": 3}', tool_call_id="1"))[:2] == (True, "")
    ok, err, _ = trace.outcome(ToolMessage(content='{"ok": false, "error": "no such page"}', tool_call_id="1"))
    assert not ok and err == "no such page"
    ok, err, res = trace.outcome(ToolMessage(content="Boom", tool_call_id="1", status="error"))
    assert not ok and err == "Boom" and res == "Boom"
    ok, _, res = trace.outcome('{"token": "abc", "rows": [1]}')
    assert ok and "abc" not in res


def test_which_tools_get_a_screenshot():
    assert trace.wants_shot("browser_navigate") and trace.wants_shot("computer_act")
    assert not trace.wants_shot("search_mail") and not trace.wants_shot("browser_logins")


def test_no_tracer_outside_a_job():
    assert trace.CURRENT.get() is None and trace.callbacks("Raaz") == []


# ---------------------------------------------------------------- against a real database (CHECKME_TEST_DATABASE_URL)

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")


@pytest.fixture
async def factory():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import _migrate_jobs
    from app.models import Base

    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_jobs(conn)
        for col in ("accepts_from", "limits", "browser"):
            await conn.execute(text(f"ALTER TABLE workspace_agents ADD COLUMN IF NOT EXISTS {col} JSONB DEFAULT '{{}}'::jsonb"))
    made: list = []
    yield async_sessionmaker(engine, expire_on_commit=False), made
    async with engine.begin() as conn:
        for uid in made:
            await conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
    await engine.dispose()


def _png() -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(out, "PNG")
    return out.getvalue()


def _llm(msg):
    return SimpleNamespace(generations=[[SimpleNamespace(message=msg)]])


@needs_db
async def test_a_job_keeps_every_step_with_why_inputs_result_and_screenshot(factory, monkeypatch):
    from app.browser_runs import runlog
    from app.jobs import service, worker
    from app.jobs.runner import run_job
    from app.models import BrowserRun, User, WorkspaceAgent
    import app.storage

    factory, made = factory
    stored: dict[str, bytes] = {}

    async def frame(user_id):
        return _png()

    monkeypatch.setattr(runlog, "latest_frame", frame)
    monkeypatch.setattr(app.storage, "put_bytes", lambda key, data, ctype: stored.__setitem__(key, data))
    monkeypatch.setattr(app.storage, "file_url", lambda key, name, ctype, inline=False: f"https://files.test/{key}")
    monkeypatch.setattr("app.db.SessionLocal", factory)

    async with factory() as db:
        user = User(email=f"{uuid.uuid4().hex[:8]}@x.test", primary_provider="google")
        db.add(user)
        await db.flush()
        made.append(user.id)
        agent = WorkspaceAgent(user_id=user.id, name="Raaz", focus="general", kind="custom")
        db.add(agent)
        await db.flush()
        other = User(email=f"{uuid.uuid4().hex[:8]}@x.test", primary_provider="google")
        db.add(other)
        await db.flush()
        made.append(other.id)
        job = await service.create_job(db, user, agent=agent, prompt="Check the price on example.com", role="worker")
        await db.commit()
        job_id = job.id
    await worker.claim(factory, 4)

    async def agent_impl(db, user_, session_id, request_id, history, query, confirmations, **kw):
        cbs = trace.callbacks("Raaz", 0, {})
        assert len(cbs) == 1  # the runner set a tracer for this turn
        cb = cbs[0]
        # Step 1: reasoning, then a browser step that gets a screenshot.
        await cb.on_chat_model_start({}, [])
        await cb.on_llm_end(_llm(AIMessage(content="I'll open the product page first.", tool_calls=[{"name": "browser_navigate", "args": {}, "id": "a"}])))
        r1 = uuid.uuid4()
        await cb.on_tool_start({"name": "browser_navigate"}, "", run_id=r1, inputs={"url": "https://example.com/p", "password": "nope"})
        await cb.on_tool_end(ToolMessage(content='{"ok": true, "title": "Product"}', tool_call_id="a"), run_id=r1)
        # Step 2: a browser_task that recorded its own browser run.
        r2 = uuid.uuid4()
        await cb.on_tool_start({"name": "browser_task"}, "", run_id=r2, inputs={"goal": "read the price"})
        async with factory() as s:
            s.add(BrowserRun(user_id=user_.id, goal="read the price", status="completed"))
            await s.commit()
        await cb.on_tool_end(ToolMessage(content='{"ok": true, "done": true, "result": "$12"}', tool_call_id="b"), run_id=r2)
        # Step 3: a teammate's failing tool, reported by its own graph.
        mate = trace.callbacks("Tony", 1, {})[0]
        await mate.on_llm_end(_llm(AIMessage(content="Checking the old price in mail.", tool_calls=[{"name": "search_mail", "args": {}, "id": "c"}])))
        r3 = uuid.uuid4()
        await mate.on_tool_start({"name": "search_mail"}, '{"query": "price"}', run_id=r3)
        await mate.on_tool_error(RuntimeError("mail is not connected"), run_id=r3)
        # Step 4: started, never finished (the turn ends first).
        await cb.on_tool_start({"name": "fetch_url"}, '{"url": "https://example.com"}', run_id=uuid.uuid4())
        # The final model message (no tool calls) is not a step by itself: the reply step is the job's result.
        await cb.on_llm_end(_llm(AIMessage(content="Thinking aloud inside a tool")))
        return {"text": "The price is $12."}

    assert await run_job(job_id, factory, agent_impl) == "done"

    async with factory() as db:
        out = await trace.tree_trace(db, await db.get(User, user.id), job_id)
    steps = out["steps"]
    assert [s["tool"] or s["kind"] for s in steps] == ["browser_navigate", "browser_task", "search_mail", "fetch_url", "reply"]
    nav, task, mail, cut, reply = steps
    assert nav["why"] == "I'll open the product page first." and nav["ok"] and nav["args"]["password"] == trace.HIDDEN
    assert nav["args"]["url"] == "https://example.com/p" and nav["agent_name"] == "Raaz" and nav["ms"] >= 0
    assert nav["shot_url"] and f"job-traces/{job_id}/1-1.jpg" in nav["shot_url"] and stored  # a screenshot was saved
    assert task["browser_run_id"] and "$12" in task["result"] and task["shot_url"]
    assert mail["agent_name"] == "Tony" and not mail["ok"] and "not connected" in mail["error"]
    assert mail["why"] == "Checking the old price in mail." and mail["shot_url"] is None
    assert not cut["ok"] and cut["error"] == "Stopped before it finished"
    assert reply["action"] == "Replied" and reply["result"] == "The price is $12."
    assert out["jobs"][0]["id"] == str(job_id)

    # The route: the owner sees it, another user gets 404.
    from fastapi import HTTPException

    from app.jobs import routes

    async with factory() as db:
        view = await routes.job_trace(str(job_id), user=await db.get(User, user.id), db=db)
        assert len(view["steps"]) == 5
        with pytest.raises(HTTPException) as missing:
            await routes.job_trace(str(job_id), user=await db.get(User, other.id), db=db)
        assert missing.value.status_code == 404


@needs_db
async def test_old_trace_screenshots_are_dropped_and_steps_kept(factory, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.models import AgentJob, JobTraceStep, User
    import app.storage

    factory, made = factory
    deleted: list = []
    monkeypatch.setattr(app.storage, "delete_keys", lambda keys: deleted.extend(keys))
    async with factory() as db:
        user = User(email=f"{uuid.uuid4().hex[:8]}@x.test", primary_provider="google")
        db.add(user)
        await db.flush()
        made.append(user.id)
        job = AgentJob(user_id=user.id, title="t", status="done")
        db.add(job)
        await db.flush()
        old = datetime.now(timezone.utc) - timedelta(days=40)
        db.add(JobTraceStep(job_id=job.id, user_id=user.id, i=1, tool="browser_navigate", shot="k/old.jpg", started_at=old))
        db.add(JobTraceStep(job_id=job.id, user_id=user.id, i=2, tool="browser_navigate", shot="k/new.jpg"))
        await db.commit()
    assert await trace.tidy_screenshots(factory, user.id) == 1 and deleted == ["k/old.jpg"]
    async with factory() as db:
        rows = (await trace.tree_trace(db, await db.get(User, user.id), job.id))["steps"]
    assert len(rows) == 2 and rows[0]["shot_url"] is None
