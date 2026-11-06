"""In-process worker for memory extract jobs. Does not use the computer job lock."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

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


def _now():
    return datetime.now(timezone.utc)


async def claim(limit: int = 4) -> list:
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(MemoryExtractJob)
                .where(MemoryExtractJob.status == "queued")
                .order_by(MemoryExtractJob.created_at)
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        ).scalars().all()
        ids = []
        for row in rows:
            row.status = "running"
            row.attempts = (row.attempts or 0) + 1
            row.updated_at = _now()
            ids.append(row.id)
        await db.commit()
        return ids


async def _run_one(job_id) -> None:
    async with SessionLocal() as db:
        job = await db.get(MemoryExtractJob, job_id)
        if job is None or job.status != "running":
            return
        try:
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
            for job_id in ids:
                await _run_one(job_id)
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
