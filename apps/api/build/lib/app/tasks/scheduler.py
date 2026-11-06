from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.db import SessionLocal
from app.models import ScheduledTask, User
from app.tasks.service import execute_task

log = logging.getLogger(__name__)
INTERVAL_SEC = 20
BATCH = 6


async def scheduler_loop() -> None:
    await asyncio.sleep(4)
    while True:
        try:
            await tick()
        except Exception:  # noqa: BLE001
            log.exception("scheduler tick failed")
        await asyncio.sleep(INTERVAL_SEC)


async def tick() -> None:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(ScheduledTask)
                .where(ScheduledTask.status == "active", ScheduledTask.next_run_at <= now)
                .order_by(ScheduledTask.next_run_at)
                .limit(BATCH)
                # The API may run more than one replica. Lock due rows while we
                # reserve them so only one scheduler instance can claim each run.
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        ids = [row.id for row in rows]
        for row in rows:
            row.status = "running"
        if ids:
            await db.commit()

    for task_id in ids:
        try:
            async with SessionLocal() as db:
                task = await db.get(ScheduledTask, task_id)
                if task is None:
                    continue
                user = await db.get(User, task.user_id)
                if user is None:
                    task.status = "paused"
                    task.last_error = "User is missing."
                    await db.commit()
                    continue
                await execute_task(db, user, task, from_scheduler=True)
        except Exception:  # noqa: BLE001
            log.exception("scheduled task %s failed", task_id)


async def start_scheduler() -> asyncio.Task:
    return asyncio.create_task(scheduler_loop(), name="task-scheduler")
