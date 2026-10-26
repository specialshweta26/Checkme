"""In-process job worker, started next to the task scheduler in the API lifespan.

- Polls every POLL_S for queued jobs and claims them with FOR UPDATE SKIP LOCKED.
- One running job per user (they share one computer/sandbox), at most MAX_CONCURRENT overall.
- A heartbeat is written every HEARTBEAT_S while a job runs; jobs whose heartbeat is older than
  STALE_S (the process died or was redeployed) are requeued, and failed after MAX_ATTEMPTS.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import and_, or_, select

from app.jobs import service
from app.jobs.runner import run_job
from app.models import AgentJob

log = logging.getLogger(__name__)

POLL_S = 3
HEARTBEAT_S = 60
STALE_S = 300
MAX_CONCURRENT = 4
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


async def claim(session_factory: Callable, limit: int, busy_users: set) -> list:
    """Mark up to `limit` queued jobs as running (oldest first, one per user)."""
    if limit <= 0:
        return []
    running = select(AgentJob.user_id).where(AgentJob.status == "running")
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(AgentJob)
                .where(AgentJob.status == "queued", AgentJob.user_id.not_in(running))
                .order_by(AgentJob.created_at)
                .with_for_update(skip_locked=True)
                .limit(limit * 4)
            )
        ).scalars().all()
        picked = []
        users = set(busy_users)
        for job in rows:
            if job.user_id in users or len(picked) >= limit:
                continue
            users.add(job.user_id)
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


async def _run_one(session_factory: Callable, job_id) -> None:
    beat = asyncio.create_task(_heartbeat(session_factory, job_id))
    try:
        status = await run_job(job_id, session_factory)
        log.info("job %s -> %s", job_id, status)
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
        beat.cancel()
        wake()  # a parent may have been requeued


async def worker_loop(session_factory: Callable | None = None) -> None:
    if session_factory is None:
        from app.db import SessionLocal

        session_factory = SessionLocal
    await asyncio.sleep(5)
    active: dict = {}  # task -> user_id
    tick = 0
    while True:
        try:
            if tick % 20 == 0:
                await recover_stale(session_factory)
            tick += 1
            for task in [t for t in active if t.done()]:
                active.pop(task, None)
            picked = await claim(session_factory, MAX_CONCURRENT - len(active), set(active.values()))
            for job_id, user_id in picked:
                task = asyncio.create_task(_run_one(session_factory, job_id), name=f"job-{job_id}")
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


async def start_job_worker() -> asyncio.Task:
    return asyncio.create_task(worker_loop(), name="job-worker")
