"""In-process worker for memory extract jobs. Does not use the computer job lock."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.agent.memory_extract import process_job
from app.db import SessionLocal
from app.models import MemoryExtractJob

log = logging.getLogger(__name__)

POLL_S = 2
MAX_ATTEMPTS = 3
_wake = asyncio.Event()


def wake() -> None:
    try:
        _wake.set()
    except RuntimeError:
        pass


STALE_S = 600  # a job running longer than this was left behind by a stopped instance


def _now():
    return datetime.now(timezone.utc)


async def claim(limit: int = 4) -> list:
    """Claim up to `limit` queued jobs, at most one per user, and none for a user whose job is already running.

    One user's memory writes then happen in order (two turns can't both replace the same fact), while different
    users are processed in parallel. FOR UPDATE SKIP LOCKED keeps several API instances from claiming the same job."""
    async with SessionLocal() as db:
        # A job left "running" by an instance that died would block its user forever: put it back in the queue.
        await db.execute(
            update(MemoryExtractJob)
            .where(MemoryExtractJob.status == "running", MemoryExtractJob.updated_at < _now() - timedelta(seconds=STALE_S))
            .values(status="queued", updated_at=_now())
        )
        running = select(MemoryExtractJob.user_id).where(MemoryExtractJob.status == "running")
        rows = (
            await db.execute(
                select(MemoryExtractJob)
                .where(MemoryExtractJob.status == "queued", MemoryExtractJob.user_id.not_in(running))
                .order_by(MemoryExtractJob.created_at)
                .with_for_update(skip_locked=True)
                .limit(limit * 4)
            )
        ).scalars().all()
        rows = one_per_user(rows, limit)
        ids = []
        for row in rows:
            row.status = "running"
            row.attempts = (row.attempts or 0) + 1
            row.updated_at = _now()
            ids.append(row.id)
        await db.commit()
        return ids


def one_per_user(rows: list, limit: int) -> list:
    """The oldest job of each user, up to `limit` users."""
    picked, seen = [], set()
    for row in rows:
        if row.user_id in seen:
            continue
        seen.add(row.user_id)
        picked.append(row)
        if len(picked) >= limit:
            break
    return picked


async def _run_one(job_id) -> None:
    async with SessionLocal() as db:
        job = await db.get(MemoryExtractJob, job_id)
        if job is None or job.status != "running":
            return
        from app.agent.meter import metered

        try:
            with metered(job.user_id, "chat"):  # remembering what was said in chat is part of chat's cost
                await process_job(db, job)
        except Exception as vis_exc:  # noqa: BLE001
            log.warning("memory extract failed: %s", vis_exc)
            if (job.attempts or 0) >= MAX_ATTEMPTS:
                job.status = "failed"
                job.last_error = str(vis_exc)[:400]
            else:
                job.status = "queued"
                job.last_error = str(vis_exc)[:400]
        job.updated_at = _now()
        await db.commit()


async def worker_loop() -> None:
    await asyncio.sleep(1)
    while True:
        try:
            ids = await claim()
            # Different users' jobs run side by side; each user has at most one in this batch.
            await asyncio.gather(*(_run_one(job_id) for job_id in ids))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("memory extract worker tick failed")
        try:
            _wake.clear()
            await asyncio.wait_for(_wake.wait(), timeout=POLL_S)
        except asyncio.TimeoutError:
            pass


async def start_memory_worker() -> asyncio.Task:
    return asyncio.create_task(worker_loop(), name="memory-extract")
