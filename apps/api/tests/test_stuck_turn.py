"""A turn that stops making progress is stopped and says why, while quiet but working turns keep going."""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4

import pytest

from app.agent import cancel as rc


def _run(**kw):
    return rc.Run(key=f"t-{uuid4().hex[:6]}", user_id="u", **kw)


def test_quiet_for_too_long_counts_as_stuck():
    run = _run()
    now = time.monotonic()
    run.last_active = now - rc.IDLE_LIMIT_S + 5
    assert not run.stalled(now)
    run.last_active = now - rc.IDLE_LIMIT_S - 5
    assert run.stalled(now)


def test_a_running_tool_gets_longer():
    run = _run()
    now = time.monotonic()
    run.tools_running.add("tool-1")
    run.last_active = now - rc.IDLE_LIMIT_S - 5
    assert not run.stalled(now)
    run.last_active = now - rc.TOOL_IDLE_LIMIT_S - 5
    assert run.stalled(now)


async def test_a_stuck_turn_is_stopped_with_a_reason(monkeypatch):
    monkeypatch.setattr(rc, "IDLE_LIMIT_S", 0.1)

    async def stuck():
        await asyncio.sleep(3600)

    run = _run()
    run.task = asyncio.create_task(stuck())

    async def no_remote(*a, **k):
        return False

    monkeypatch.setattr(rc, "remote_requested", no_remote)
    await asyncio.wait_for(rc.watch(run, every_s=0.02), 2)
    assert run.cancelled and run.reason == rc.NO_PROGRESS
    with pytest.raises(asyncio.CancelledError):
        await run.task


async def test_activity_keeps_a_turn_alive(monkeypatch):
    monkeypatch.setattr(rc, "IDLE_LIMIT_S", 0.15)

    async def no_remote(*a, **k):
        return False

    monkeypatch.setattr(rc, "remote_requested", no_remote)
    run = _run()
    watcher = asyncio.create_task(rc.watch(run, every_s=0.02))
    for _ in range(10):  # 0.5s in all, well past the limit, but never quiet for long
        await asyncio.sleep(0.05)
        run.touch()
    assert not run.cancelled
    watcher.cancel()


async def test_a_broken_redis_does_not_turn_off_the_stuck_check(monkeypatch):
    monkeypatch.setattr(rc, "IDLE_LIMIT_S", 0.15)

    async def broken(*a, **k):
        raise RuntimeError("redis down")

    monkeypatch.setattr(rc, "remote_requested", broken)
    run = _run()
    await asyncio.wait_for(rc.watch(run, every_s=0.02), 2)
    assert run.reason == rc.NO_PROGRESS


async def test_jobs_are_left_to_their_own_watch(monkeypatch):
    monkeypatch.setattr(rc, "IDLE_LIMIT_S", 0.05)

    async def no_remote(*a, **k):
        return False

    monkeypatch.setattr(rc, "remote_requested", no_remote)
    run = _run(kind="job")
    watcher = asyncio.create_task(rc.watch(run, every_s=0.02))
    await asyncio.sleep(0.2)
    assert not run.cancelled
    watcher.cancel()


async def test_model_and_tool_events_count_as_progress_and_track_tools():
    run = _run()
    token = rc.current.set(run)
    try:
        cb = rc.callback()
    finally:
        rc.current.reset(token)
    run.last_active = 0
    await cb.on_llm_new_token("hi")
    assert run.last_active > 0
    await cb.on_tool_start({}, "", run_id="r1")
    await cb.on_tool_start({}, "", run_id="r2")
    assert run.tools_running == {"r1", "r2"}
    await cb.on_tool_end("ok", run_id="r1")
    await cb.on_tool_error(RuntimeError("x"), run_id="r2")
    assert run.tools_running == set()


async def test_a_status_line_counts_as_progress():
    from app.agent.progress import ProgressReporter
    from tests.test_progress import FakeRedis

    run = _run()
    run.last_active = 0
    token = rc.current.set(run)
    try:
        await ProgressReporter("u", "turn_stuck0001", redis_client=FakeRedis()).emit("Reading your message")
    finally:
        rc.current.reset(token)
    assert run.last_active > 0


def test_the_stop_message_says_why():
    assert "nothing happened for several minutes" in rc.stopped_text("", ["Recalling"], rc.NO_PROGRESS)
    assert "nothing happened" not in rc.stopped_text("", ["Recalling"], "stopped")


async def test_a_real_turn_stuck_in_the_model_call_is_stopped(monkeypatch):
    """The whole agent turn, with a model call that never returns, the way the chat route runs it."""
    import uuid
    from types import SimpleNamespace

    import app.connectors.platform as platform
    from app.agent import deep

    monkeypatch.setattr(rc, "IDLE_LIMIT_S", 0.3)

    async def none(*a, **k):
        return None

    async def empty(*a, **k):
        return {}

    async def nothing(*a, **k):
        return []

    class StuckGraph:
        async def ainvoke(self, payload, config=None, **kw):
            await asyncio.sleep(3600)

    async def no_remote(*a, **k):
        return False

    monkeypatch.setattr(platform, "ensure_platform_connectors", none)
    monkeypatch.setattr(deep, "user_connectors", nothing)
    monkeypatch.setattr(deep, "connector_assignment_map", empty)
    monkeypatch.setattr(deep, "toolkit_assignment_map", empty)
    monkeypatch.setattr(deep, "_maybe_start_connect", none)
    monkeypatch.setattr(deep, "build_agent", lambda ctx, prompt: StuckGraph())
    monkeypatch.setattr(rc, "remote_requested", no_remote)

    run = rc.register(f"stuck-{uuid.uuid4().hex[:6]}", user_id="u", kind="turn")
    token = rc.current.set(run)
    try:
        turn = asyncio.create_task(
            deep.run_deep_agent(None, SimpleNamespace(id="u", timezone="Asia/Kolkata"), uuid.uuid4(), "r", [], "write me a long report")
        )
    finally:
        rc.current.reset(token)
    run.task = turn
    started = time.monotonic()
    await asyncio.wait_for(rc.watch(run, every_s=0.05), 5)
    with pytest.raises((asyncio.CancelledError, rc.TurnCancelled)):
        await asyncio.wait_for(turn, 5)
    assert run.reason == rc.NO_PROGRESS and time.monotonic() - started < 5
    rc.unregister(run.key, run)
