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


def test_a_restart_matches_its_thread_and_a_reply_starts_a_new_one():
    job = SimpleNamespace(id=uuid.uuid4())
    first = run_store.job_thread(job, "do it", [], [])
    assert first == run_store.job_thread(job, "do it", [], [])
    assert first != run_store.job_thread(job, "do it", [{"token": "t"}], [])
    assert first != run_store.job_thread(job, "next step", [], [])
    assert first != run_store.job_thread(SimpleNamespace(id=uuid.uuid4()), "do it", [], [])


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

    from app.models import Base

    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
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
