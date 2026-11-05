"""Jobs run side by side, and only jobs that use the computer take turns on it."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.connectors.runtime import RuntimeConnector
from app.jobs import computer_lock, worker


def _job(user):
    return SimpleNamespace(user_id=user)


def test_pick_jobs_runs_several_per_user_up_to_the_limit():
    a, b = uuid.uuid4(), uuid.uuid4()
    queued = [_job(a) for _ in range(5)] + [_job(b)]
    picked = worker.pick_jobs(queued, [], 4)
    assert [j.user_id for j in picked] == [a, a, a, b]


def test_pick_jobs_counts_jobs_already_running():
    a = uuid.uuid4()
    assert worker.pick_jobs([_job(a)], [a] * worker.MAX_PER_USER, 4) == []
    assert len(worker.pick_jobs([_job(a), _job(a)], [a], 4)) == 2
    assert len(worker.pick_jobs([_job(a), _job(a)], [], 1)) == 1


async def test_second_job_waits_for_the_computer_and_reports_it():
    user, waits = uuid.uuid4(), []

    async def on_wait():
        waits.append(1)

    first = computer_lock.JobComputer(user)
    second = computer_lock.JobComputer(user, on_wait)
    await first.ensure()
    waiting = asyncio.create_task(second.ensure())
    await asyncio.sleep(0.01)
    assert not waiting.done() and waits == [1]
    first.release()
    await asyncio.wait_for(waiting, 1)
    assert second.held
    second.release()


async def test_other_users_do_not_wait():
    first = computer_lock.JobComputer(uuid.uuid4())
    other = computer_lock.JobComputer(uuid.uuid4())
    await first.ensure()
    await asyncio.wait_for(other.ensure(), 1)
    first.release()
    other.release()


async def test_parallel_calls_in_one_job_take_the_lock_once():
    claim = computer_lock.JobComputer(uuid.uuid4())
    await asyncio.wait_for(asyncio.gather(claim.ensure(), claim.ensure(), claim.ensure()), 1)
    assert claim.held
    claim.release()
    assert not claim._lock.locked()


async def test_stopping_a_waiting_job_leaves_the_lock_usable():
    user = uuid.uuid4()
    first, second, third = (computer_lock.JobComputer(user) for _ in range(3))
    await first.ensure()
    waiting = asyncio.create_task(second.ensure())
    await asyncio.sleep(0.01)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    second.release()  # what the worker does when the job ends
    assert first.held and first._lock.locked()
    first.release()
    await asyncio.wait_for(third.ensure(), 1)
    third.release()


async def test_outside_a_job_ensure_does_nothing():
    await asyncio.wait_for(computer_lock.ensure(), 1)


@pytest.mark.parametrize(("provider", "patch_target"), [("daytona", "app.connectors.daytona_box.call_daytona"), ("computer_local", "app.desktop.agent.call_local")])
async def test_computer_calls_take_the_claim(monkeypatch, provider, patch_target):
    async def fake(*a, **k):
        return {"ok": True}

    monkeypatch.setattr(patch_target, fake)
    row = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), kind="builtin", provider=provider, alias="", name="c", slug="c")
    claim = computer_lock.JobComputer(uuid.uuid4())
    token = computer_lock.current.set(claim)
    try:
        await RuntimeConnector(row).call("exec", {})
    finally:
        computer_lock.current.reset(token)
    assert claim.held
    claim.release()
