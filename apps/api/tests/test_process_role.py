"""With web and workers split (PROCESS_ROLE), a new-mail notification received by the web process reaches the worker
over Redis; in a single process ("all") it wakes the inbox loop directly."""

from __future__ import annotations

import asyncio
import json
import uuid

from app.config import get_settings
from app.inbox import worker


async def test_web_role_sends_the_wake_to_the_worker(monkeypatch):
    sent = []

    async def publish(ids):
        sent.append(ids)

    monkeypatch.setattr(get_settings(), "process_role", "web")
    monkeypatch.setattr(worker, "_publish_wake", publish)
    worker._urgent.clear()
    worker._wake.clear()
    uid = uuid.uuid4()
    worker.wake_inbox({uid})
    await asyncio.sleep(0)
    assert sent == [[uid]] and worker._urgent == set() and not worker._wake.is_set()


async def test_one_process_wakes_its_own_loop(monkeypatch):
    monkeypatch.setattr(get_settings(), "process_role", "all")
    worker._urgent.clear()
    worker._wake.clear()
    uid = uuid.uuid4()
    worker.wake_inbox({uid})
    assert worker._urgent == {uid} and worker._wake.is_set()


async def test_the_worker_turns_redis_messages_into_checks(monkeypatch):
    uid = uuid.uuid4()

    class PubSub:
        async def subscribe(self, channel):
            assert channel == worker.WAKE_CHANNEL

        async def listen(self):
            yield {"type": "subscribe"}
            yield {"type": "message", "data": json.dumps([str(uid)])}
            yield {"type": "message", "data": "not json"}
            await asyncio.sleep(3600)

    monkeypatch.setattr("app.cache.get_redis", lambda: type("R", (), {"pubsub": lambda self: PubSub()})())
    worker._urgent.clear()
    worker._wake.clear()
    task = asyncio.create_task(worker.wake_listener())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert worker._urgent == {uid} and worker._wake.is_set()
    worker._urgent.clear()
    worker._wake.clear()
