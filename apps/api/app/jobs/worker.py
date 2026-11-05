"""In-process job worker, started next to the task scheduler in the API lifespan.

- Polls every POLL_S for queued jobs and claims them with FOR UPDATE SKIP LOCKED.
- Up to MAX_PER_USER running jobs per user, at most MAX_CONCURRENT overall. Jobs that use the computer take turns on
  it (see computer_lock), so a long coding job doesn't delay a job that only needs connectors.
- A heartbeat is written every HEARTBEAT_S while a job runs; jobs whose heartbeat is older than
  STALE_S (the process died or was redeployed) are requeued, and failed after MAX_ATTEMPTS.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from sqlalchemy import and_, or_, select

from app.jobs import computer_lock, service
from app.jobs.runner import run_job
from app.models import AgentJob

log = logging.getLogger(__name__)

POLL_S = 3
LOGIN_POLL_TICKS = 5  # jobs waiting on a CLI sign-in are checked about every 15 s
HEARTBEAT_S = 60
STALE_S = 300
MAX_CONCURRENT = 4
MAX_PER_USER = 3  # leaves a slot for other users, since all jobs share MAX_CONCURRENT
MAX_ATTEMPTS = 3

_wake = asyncio.Event()


def wake() -> None:
    """Nudge the worker after queueing a job so it starts without waiting a full poll."""
    try:
        _wake.set()
    except RuntimeError:
        pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def recover_stale(session_factory: Callable) -> int:
    cutoff = _now() - timedelta(seconds=STALE_S)
    moved = 0
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(AgentJob)
                .where(
                    AgentJob.status == "running",
                    or_(
                        AgentJob.heartbeat_at < cutoff,
                        and_(AgentJob.heartbeat_at.is_(None), AgentJob.started_at < cutoff),
                    ),
                )
                .with_for_update(skip_locked=True)
                .limit(20)
            )
        ).scalars().all()
        for job in rows:
            moved += 1
            if (job.attempts or 0) >= MAX_ATTEMPTS:
                db.add(service.fail(job, "The worker stopped while running this job too many times."))
                await service.after_terminal(db, job)
            else:
                job.status = "queued"
                db.add(service.event(job, "status", "Restarting after an interruption", {"status": "queued"}))
        await db.commit()
    return moved


def pick_jobs(queued: Iterable, running_users: Iterable, limit: int) -> list:
    """The queued jobs to start, oldest first, without passing MAX_PER_USER for any user."""
    counts = Counter(running_users)
    picked = []
    for job in queued:
        if len(picked) >= limit:
            break
        if counts[job.user_id] >= MAX_PER_USER:
            continue
        counts[job.user_id] += 1
        picked.append(job)
    return picked


async def claim(session_factory: Callable, limit: int) -> list:
    """Mark up to `limit` queued jobs as running (oldest first, up to MAX_PER_USER per user)."""
    if limit <= 0:
        return []
    async with session_factory() as db:
        running = (await db.execute(select(AgentJob.user_id).where(AgentJob.status == "running"))).scalars().all()
        rows = (
            await db.execute(
                select(AgentJob)
                .where(AgentJob.status == "queued")
                .order_by(AgentJob.created_at)
                .with_for_update(skip_locked=True)
                .limit(limit * 4)
            )
        ).scalars().all()
        picked = []
        for job in pick_jobs(rows, running, limit):
            job.status = "running"
            job.attempts = (job.attempts or 0) + 1
            job.started_at = job.started_at or _now()
            job.heartbeat_at = _now()
            picked.append((job.id, job.user_id))
        await db.commit()
    return picked


async def _heartbeat(session_factory: Callable, job_id) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_S)
        try:
            async with session_factory() as db:
                job = await db.get(AgentJob, job_id)
                if job is None or job.status != "running":
                    return
                job.heartbeat_at = _now()
                await db.commit()
        except Exception as exc:  # noqa: BLE001
            log.debug("heartbeat failed: %s", exc)


CANCEL_WATCH_S = 3


async def _watch_cancel(session_factory: Callable, job_id, run) -> None:
    """Cooperative stop for jobs cancelled from another process (or a DB-only cancel): poll the status."""
    while not run.cancelled:
        await asyncio.sleep(CANCEL_WATCH_S)
        try:
            async with session_factory() as db:
                job = await db.get(AgentJob, job_id)
                status = getattr(job, "status", None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.debug("cancel watch failed: %s", exc)
            continue
        if status == "cancelled":
            run.cancel("cancelled")
            return
        if status is None or status in service.TERMINAL:
            return


async def _run_one(session_factory: Callable, job_id, user_id=None) -> None:
    from app.agent import cancel as run_cancel

    async def report_wait() -> None:
        async with session_factory() as db:
            job = await db.get(AgentJob, job_id)
            if job is not None:
                db.add(service.event(job, "status", "Waiting for the computer; another job is using it"))
                await db.commit()

    beat = asyncio.create_task(_heartbeat(session_factory, job_id))
    run = run_cancel.register(run_cancel.job_key(job_id), user_id=user_id, kind="job", job_id=job_id)
    computer = computer_lock.JobComputer(user_id, report_wait)
    token = run_cancel.current.set(run)
    computer_token = computer_lock.current.set(computer)
    try:
        inner = asyncio.create_task(run_job(job_id, session_factory), name=f"job-run-{job_id}")
    finally:
        computer_lock.current.reset(computer_token)
        run_cancel.current.reset(token)
    run.task = inner
    watch = asyncio.create_task(_watch_cancel(session_factory, job_id, run))
    try:
        try:
            status = await inner
        except asyncio.CancelledError:
            inner.cancel()  # the worker itself is stopping
            raise
        log.info("job %s -> %s", job_id, status)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("job %s crashed", job_id)
        try:
            async with session_factory() as db:
                job = await db.get(AgentJob, job_id)
                if job is not None and job.status == "running":
                    db.add(service.fail(job, "The job crashed."))
                    await service.after_terminal(db, job)
                    await db.commit()
        except Exception:  # noqa: BLE001
            log.exception("could not mark job %s failed", job_id)
    finally:
        computer.release()
        beat.cancel()
        watch.cancel()
        run_cancel.unregister(run.key, run)
        wake()  # a parent may have been requeued


async def worker_loop(session_factory: Callable | None = None) -> None:
    if session_factory is None:
        from app.db import SessionLocal

        session_factory = SessionLocal
    await asyncio.sleep(5)
    active: dict = {}  # task -> user_id
    tick = 0
    login_poll: asyncio.Task | None = None
    while True:
        try:
            if tick % 20 == 0:
                await recover_stale(session_factory)
            if tick % 1200 == 0:  # about hourly
                from app.agent.run_store import prune_old

                await prune_old(session_factory)
            if tick % LOGIN_POLL_TICKS == 0 and (login_poll is None or login_poll.done()):
                login_poll = asyncio.create_task(_poll_logins(session_factory), name="login-poll")
            tick += 1
            for task in [t for t in active if t.done()]:
                active.pop(task, None)
            picked = await claim(session_factory, MAX_CONCURRENT - len(active))
            for job_id, user_id in picked:
                task = asyncio.create_task(_run_one(session_factory, job_id, user_id), name=f"job-{job_id}")
                active[task] = user_id
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("job worker tick failed")
        _wake.clear()
        try:
            await asyncio.wait_for(_wake.wait(), timeout=POLL_S)
        except asyncio.TimeoutError:
            pass


async def _poll_logins(session_factory: Callable) -> None:
    """Resume jobs whose user finished a CLI browser / device sign-in (see app.agent.cli_login)."""
    from app.agent import cli_login

    try:
        if await cli_login.poll_waiting_logins(session_factory):
            wake()
    except Exception:  # noqa: BLE001
        log.exception("login poll failed")


async def start_job_worker() -> asyncio.Task:
    return asyncio.create_task(worker_loop(), name="job-worker")
