"""A slow memory store costs a few seconds, not the whole turn."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from types import SimpleNamespace

import pytest

from app.agent import deep
from app.agent.fast_path import SLOW_STAGE_MS, TurnTimer

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")


async def test_a_stuck_memory_store_does_not_hang_the_turn(monkeypatch, caplog):
    monkeypatch.setattr(deep, "MEMORY_TIMEOUT_S", 0.05)

    async def stuck(*a, **k):
        await asyncio.sleep(3600)

    monkeypatch.setattr(deep, "load_memory", stuck)
    with caplog.at_level(logging.WARNING):
        assert await asyncio.wait_for(deep._load_memory_bounded(None, None, "hi", None, ""), 2) == ""
    assert "timed out" in caplog.text


async def test_a_failing_memory_store_answers_without_it(monkeypatch):
    async def broken(*a, **k):
        raise RuntimeError("store down")

    monkeypatch.setattr(deep, "load_memory", broken)
    assert await deep._load_memory_bounded(None, None, "hi", None, "") == ""


async def test_memory_comes_back_when_it_is_quick(monkeypatch):
    async def quick(*a, **k):
        return "the user likes tea"

    monkeypatch.setattr(deep, "load_memory", quick)
    assert await deep._load_memory_bounded(SimpleNamespace(), None, "hi", None, "") == "the user likes tea"


async def test_stop_still_works_during_recall(monkeypatch):
    async def stuck(*a, **k):
        await asyncio.sleep(3600)

    monkeypatch.setattr(deep, "load_memory", stuck)
    task = asyncio.create_task(deep._load_memory_bounded(None, None, "hi", None, ""))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")
async def test_being_cut_off_leaves_the_turns_database_session_usable(monkeypatch):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    engine = create_async_engine(URL)
    monkeypatch.setattr(deep, "MEMORY_TIMEOUT_S", 0.2)

    async def slow_query(own, *a, **k):
        assert own is not turn_db
        await own.execute(text("SELECT pg_sleep(30)"))

    monkeypatch.setattr(deep, "load_memory", slow_query)
    async with AsyncSession(engine) as turn_db:
        started = time.monotonic()
        assert await deep._load_memory_bounded(turn_db, None, "hi", None, "") == ""
        assert time.monotonic() - started < 5
        assert (await turn_db.execute(text("SELECT 1"))).scalar() == 1
    await engine.dispose()


def test_a_slow_stage_is_logged_even_if_the_turn_never_ends(caplog):
    timer = TurnTimer()
    with caplog.at_level(logging.WARNING):
        timer.mark("memory", time.monotonic() - SLOW_STAGE_MS / 1000 - 1)
        timer.mark("setup", time.monotonic())
    assert "slow turn stage: memory" in caplog.text and "setup" not in caplog.text


def test_model_calls_cannot_wait_forever(monkeypatch):
    from app import llm

    monkeypatch.setattr(llm, "_client", None)
    monkeypatch.setattr(llm, "get_settings", lambda: SimpleNamespace(openai_api_key="k"))
    client = llm.openai_client()
    assert client.timeout == 60 and client.max_retries == 1
    monkeypatch.setattr(llm, "_client", None)
