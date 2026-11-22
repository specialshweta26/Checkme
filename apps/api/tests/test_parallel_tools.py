"""Parallel tool calls (docs/LLD.md "Parallel tool calls"): tools the model asks for in one step run at the same time,
and the turn's one database session stays safe while they do."""

from __future__ import annotations

import asyncio
import os
import time

import pytest
from sqlalchemy import text

from app.agent import deep
from app.agent.serial_db import SerialSession, serial

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")


async def test_tool_calls_in_one_step_run_at_the_same_time():
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.tools import StructuredTool

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kw):  # noqa: ANN001
            return self

    async def slow(name: str) -> str:
        """Wait a moment."""
        await asyncio.sleep(0.5)
        return name

    calls = [{"id": f"c{i}", "name": "slow", "args": {"name": f"t{i}"}} for i in range(3)]
    agent = create_agent(Model(responses=[AIMessage(content="", tool_calls=calls), AIMessage(content="done")]),
                         tools=[StructuredTool.from_function(coroutine=slow, name="slow", description="wait")])  # fmt: skip
    started = time.monotonic()
    await agent.ainvoke({"messages": [("user", "go")]})
    assert time.monotonic() - started < 1.2  # three 0.5s calls, not 1.5s


def test_every_run_context_gets_the_safe_session():
    ctx = deep.RunContext(db=object(), user=None, connectors={})
    assert isinstance(ctx.db, SerialSession)
    assert deep.RunContext(db=None, user=None, connectors={}).db is None
    assert serial(ctx.db) is ctx.db  # a teammate's context reuses it, with the same lock


async def test_statements_take_turns_and_nested_calls_do_not_deadlock():
    running, peak = 0, 0

    class Fake:
        name = "fake"

        async def execute(self, *_):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.05)
            running -= 1
            return "ok"

        def add(self, row):
            self.added = row

    db = serial(Fake())
    assert await asyncio.gather(*(db.execute("q") for _ in range(4))) == ["ok"] * 4 and peak == 1
    db.add(1)
    assert db.added == 1 and db.name == "fake"


@pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")
async def test_a_real_session_survives_parallel_statements():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(URL)
    try:
        async with async_sessionmaker(engine)() as raw:
            db = serial(raw)
            got = await asyncio.gather(*(db.execute(text("select pg_sleep(0.05), CAST(:n AS integer)"), {"n": n}) for n in range(5)))
            assert [r.all()[0][1] for r in got] == list(range(5))
    finally:
        await engine.dispose()
