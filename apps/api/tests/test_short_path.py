"""Simple requests skip memory search and skills, and a stalled event loop gets reported."""

from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace

import pytest

from app import loop_watch
from app.agent import fast_path as fp


@pytest.mark.parametrize("text", ["hi", "Hello!", "hey team", "thanks", "good morning", "how are you?", "ok"])
def test_greetings_are_simple(text):
    assert fp.is_small_talk(text) and fp.is_simple_turn(text)


@pytest.mark.parametrize("text", ["list folder in downloads", "list folder in my dekstop", "open organic_farming folder and list files", "list files on my desktop"])
def test_file_requests_are_simple(text):
    assert fp.is_simple_turn(text), text


@pytest.mark.parametrize("text", ["generate the OMS order report", "hi, can you email Asha the invoice", "what did we decide about pricing", "find 10 running shoes on myntra"])
def test_real_work_is_not_simple(text):
    assert not fp.is_simple_turn(text), text


def test_uploads_and_approvals_always_get_the_full_path():
    assert not fp.is_simple_turn("hi", has_uploads=True)
    assert not fp.is_simple_turn("list files", confirmations=True)


async def test_light_memory_keeps_the_persona_and_searches_nothing(monkeypatch):
    from app.agent import memory

    async def owner(db, user, agent=None):
        return SimpleNamespace(id="a", letta_agent_id="letta")

    class Blocks:
        def scalars(self):
            return self

        def all(self):
            return []

    class Db:
        async def execute(self, q):
            return Blocks()

    async def boom(*a, **k):
        raise AssertionError("a light load must not search")

    monkeypatch.setattr(memory, "_memory_agent", owner)
    monkeypatch.setattr(memory, "_assemble_memory", lambda blocks, facts, history: "persona")
    for name in ("embed_one", "_agent_passages", "_read_letta"):
        monkeypatch.setattr(memory, name, boom, raising=False)
    monkeypatch.setattr("app.memory_engine.recall.recall_block", boom)
    assert await memory.load_memory(Db(), SimpleNamespace(id="u"), "hi", light=True) == "persona"


def test_reports_only_real_stalls_and_not_every_check():
    assert not loop_watch.should_report(1.0, float("-inf"), 100.0)
    assert loop_watch.should_report(3.0, float("-inf"), 100.0)
    assert not loop_watch.should_report(3.0, 90.0, 100.0)
    assert loop_watch.should_report(3.0, 60.0, 100.0)


def test_a_stall_names_where_the_loop_is_stuck(monkeypatch, caplog):
    monkeypatch.setattr(loop_watch, "CHECK_S", 0.02)
    monkeypatch.setattr(loop_watch, "STALL_S", 0.05)
    state = {"beat": time.monotonic()}
    stop = threading.Event()

    def blocked_call_here():
        time.sleep(0.4)

    with caplog.at_level(logging.WARNING):
        thread = threading.Thread(target=loop_watch._watch, args=(state, threading.get_ident(), stop), daemon=True)
        thread.start()
        blocked_call_here()
        stop.set()
        thread.join(1)
    assert "event loop stalled" in caplog.text and "blocked_call_here" in caplog.text


async def test_a_healthy_loop_reports_nothing(monkeypatch, caplog):
    import asyncio

    monkeypatch.setattr(loop_watch, "CHECK_S", 0.02)
    monkeypatch.setattr(loop_watch, "BEAT_S", 0.01)
    monkeypatch.setattr(loop_watch, "STALL_S", 0.3)
    with caplog.at_level(logging.WARNING):
        task = asyncio.create_task(loop_watch.watch_loop())
        await asyncio.sleep(0.15)
        task.cancel()
    assert "stalled" not in caplog.text
