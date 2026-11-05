"""A job restarted mid-run continues from its checkpoint, and calls cut off by the restart don't run twice."""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from deepagents import create_deep_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from sqlalchemy import text

from app.agent import deep, run_store

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")


def test_each_job_has_one_thread():
    job = SimpleNamespace(id=uuid.uuid4())
    assert run_store.job_thread(job) == f"job:{job.id}"


async def test_an_approved_action_is_claimed_only_once():
    commits = []

    async def commit():
        commits.append(1)

    ctx = SimpleNamespace(job=SimpleNamespace(meta={}), db=SimpleNamespace(commit=commit))
    payload = {"slug": "mail", "tool": "send", "args": {"to": "a"}}
    assert await run_store.claim_confirmed(ctx, payload)
    assert commits == [1]  # saved before the action runs
    assert not await run_store.claim_confirmed(ctx, payload)
    assert await run_store.claim_confirmed(ctx, {**payload, "args": {"to": "b"}})


async def test_chat_turns_do_not_track_approved_actions():
    assert await run_store.claim_confirmed(SimpleNamespace(job=None, db=None), {"slug": "s"})


def test_a_finished_turn_clears_the_restart_marks():
    from app.jobs import service

    job = SimpleNamespace(id=uuid.uuid4(), root_id=None, status="running", meta={"restarted": True, "confirmed_started": ["k"]}, heartbeat_at=None, parent_id=None, kind="scheduled", result="", finished_at=None, depth=1, user_id=uuid.uuid4())
    service.apply_outcome(job, "q", {"text": "done"})
    assert "restarted" not in job.meta and "confirmed_started" not in job.meta


def test_only_top_level_jobs_are_durable(monkeypatch):
    monkeypatch.setattr(run_store, "_session_factory", lambda: None)
    assert deep._durable_saver(None) is None
    assert deep._durable_saver(SimpleNamespace(job=None, teammate_depth=0)) is None
    assert deep._durable_saver(SimpleNamespace(job=object(), teammate_depth=1)) is None
    assert isinstance(deep._durable_saver(SimpleNamespace(job=object(), teammate_depth=0)), run_store.DurableSaver)


class _Model(GenericFakeChatModel):
    def bind_tools(self, tools, **kw):
        return self


class Crash(BaseException):
    """Stands in for the process dying."""


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
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _call(name: str, n: int, call_id: str) -> dict:
    return {"name": name, "args": {"n": n}, "id": call_id, "type": "tool_call"}


@needs_db
async def test_a_restart_continues_without_repeating_finished_or_cut_off_calls(factory):
    sent: list[int] = []
    crash = {"on": True}

    @tool
    def send_email(n: int) -> str:
        """Send an email."""
        sent.append(n)
        return f"sent {n}"

    @tool
    def slow_write(n: int) -> str:
        """Change something."""
        if crash["on"]:
            raise Crash()
        sent.append(n * 100)
        return "written"

    def build(messages):
        return create_deep_agent(model=_Model(messages=iter(messages)), tools=[send_email, slow_write], system_prompt="x", checkpointer=run_store.DurableSaver(factory))

    thread = {"configurable": {"thread_id": f"job:{uuid.uuid4()}:t"}}
    first = build([
        AIMessage(content="", tool_calls=[_call("send_email", 1, "c1")]),
        AIMessage(content="", tool_calls=[_call("slow_write", 2, "c2")]),
    ])  # fmt: skip
    with pytest.raises(Crash):
        await first.ainvoke({"messages": [("user", "go")]}, config=thread, durability="sync")
    assert sent == [1]

    crash["on"] = False
    second = build([AIMessage(content="", tool_calls=[_call("send_email", 3, "c3")]), AIMessage(content="all done")])
    assert await deep._can_resume(second, thread)
    assert await run_store.settle_interrupted_calls(second, thread) == 1
    result = await second.ainvoke(None, config=thread, durability="sync")
    assert result["messages"][-1].content == "all done"
    assert sent == [1, 3]  # 1 not repeated, the cut-off write not rerun
    assert "go" in [m.content for m in result["messages"]]  # the user's request survives the reload
    cut_off = [m for m in result["messages"] if getattr(m, "tool_call_id", "") == "c2"]
    assert len(cut_off) == 1 and "may or may not" in cut_off[0].content

    await second.checkpointer.adelete_thread(thread["configurable"]["thread_id"])
    assert not await deep._can_resume(second, thread)


@needs_db
async def test_a_failed_save_does_not_fail_the_run():
    def broken():
        raise RuntimeError("database down")

    @tool
    def ping() -> str:
        """Ping."""
        return "pong"

    graph = create_deep_agent(
        model=_Model(messages=iter([AIMessage(content="", tool_calls=[{"name": "ping", "args": {}, "id": "c1", "type": "tool_call"}]), AIMessage(content="ok")])),
        tools=[ping],
        system_prompt="x",
        checkpointer=run_store.DurableSaver(broken),
    )
    result = await graph.ainvoke({"messages": [("user", "go")]}, config={"configurable": {"thread_id": "t"}}, durability="sync")
    assert result["messages"][-1].content == "ok"


@needs_db
async def test_an_approval_continues_the_same_run_with_its_context(factory):
    @tool
    def prepare_send(n: int) -> str:
        """Prepare an email."""
        return "awaiting_user_confirmation"

    job = SimpleNamespace(id=uuid.uuid4(), meta={})
    thread = {"configurable": {"thread_id": run_store.job_thread(job)}}

    def build(messages):
        return create_deep_agent(model=_Model(messages=iter(messages)), tools=[prepare_send], system_prompt="x", checkpointer=run_store.DurableSaver(factory))

    payload = {"messages": [("user", "email Asha")]}
    first = build([AIMessage(content="", tool_calls=[_call("prepare_send", 1, "c1")]), AIMessage(content="Waiting for your OK.")])
    assert await run_store.job_turn_input(first, thread, job, payload, []) == (payload, "new")
    await first.ainvoke(payload, config=thread, durability="sync")

    from langchain_core.messages import HumanMessage

    new = [HumanMessage(content="The user confirmed send. Result: sent."), HumanMessage(content="The user approved. Continue.")]
    second = build([AIMessage(content="All sent.")])
    next_input, how = await run_store.job_turn_input(second, thread, job, {"messages": [("user", "text history")]}, new)
    assert how == "continue"
    result = await second.ainvoke(next_input, config=thread, durability="sync")
    contents = [m.content for m in result["messages"]]
    assert "email Asha" in contents and "awaiting_user_confirmation" in contents  # the earlier work is still there
    assert "text history" not in contents and contents[-1] == "All sent."

    await run_store.DurableSaver(factory).adelete_thread(thread["configurable"]["thread_id"])
    assert (await run_store.job_turn_input(second, thread, job, payload, new))[1] == "new"


@needs_db
async def test_a_restart_is_told_apart_from_a_new_turn(factory):
    @tool
    def ping(n: int) -> str:
        """Ping."""
        raise Crash()

    job = SimpleNamespace(id=uuid.uuid4(), meta={})
    thread = {"configurable": {"thread_id": run_store.job_thread(job)}}
    graph = create_deep_agent(model=_Model(messages=iter([AIMessage(content="", tool_calls=[_call("ping", 1, "c1")]), AIMessage(content="ok")])), tools=[ping], system_prompt="x", checkpointer=run_store.DurableSaver(factory))
    with pytest.raises(Crash):
        await graph.ainvoke({"messages": [("user", "go")]}, config=thread, durability="sync")
    job.meta = {"restarted": True}
    assert await run_store.job_turn_input(graph, thread, job, {}, ["new"]) == (None, "restart")
    result = await graph.ainvoke(None, config=thread, durability="sync")
    assert result["messages"][-1].content == "ok"


@needs_db
async def test_pruning_removes_only_whole_idle_threads(factory):
    from datetime import datetime, timedelta, timezone

    from app.models import RunCheckpoint

    old = datetime.now(timezone.utc) - timedelta(days=run_store.KEEP_DAYS + 1)
    now = datetime.now(timezone.utc)
    idle, active = f"job:{uuid.uuid4()}", f"job:{uuid.uuid4()}"

    def row(thread, cid, when):
        return RunCheckpoint(thread_id=thread, ns="", checkpoint_id=cid, checkpoint_type="t", checkpoint=b"", meta=b"", updated_at=when)

    async with factory() as db:
        db.add_all([row(idle, "1", old), row(active, "1", old), row(active, "2", now)])
        await db.commit()
    await run_store.prune_old(factory)
    async with factory() as db:
        left = (await db.execute(text("select thread_id, checkpoint_id from run_checkpoints where thread_id in (:a, :b) order by 2"), {"a": idle, "b": active})).all()
    assert [tuple(r) for r in left] == [(active, "1"), (active, "2")]
    await run_store.DurableSaver(factory).adelete_thread(active)
