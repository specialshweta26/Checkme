from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.deep import run_deep_agent
from app.models import ChatMessage, ChatSession, ScheduledTask, User
from app.routing.describe import mentioned_connectors
from app.routing.select import user_connectors
from app.security import random_token
from app.tasks.parse import fallback_once, parse_schedule
from connector_core.calendar import as_local, format_local
from connector_core.text import clip

log = logging.getLogger(__name__)


def serialize(task: ScheduledTask, tz: str) -> dict:
    return {
        "id": str(task.id),
        "title": task.title,
        "instruction": task.instruction,
        "prompt": task.prompt,
        "schedule_kind": task.schedule_kind,
        "schedule_label": task.schedule_label,
        "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
        "next_run_label": format_local(task.next_run_at, tz) if task.next_run_at else "—",
        "last_run_at": task.last_run_at.isoformat() if task.last_run_at else None,
        "last_run_label": format_local(task.last_run_at, tz) if task.last_run_at else "—",
        "last_result": task.last_result or "",
        "last_error": task.last_error or "",
        "status": task.status,
        "connected_apps": list(task.connected_apps or []),
        "run_count": task.run_count or 0,
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }


async def create_from_prompt(db: AsyncSession, user: User, prompt: str, *, require_time: bool = False) -> dict | None:
    text = (prompt or "").strip()
    if not text:
        raise ValueError("Write what you want done and when.")
    tz = user.timezone or "Asia/Kolkata"
    parsed = await parse_schedule(text, tz)
    if not parsed.get("timed"):
        if require_time:
            return None
        parsed = fallback_once(text, tz)
    if not parsed.get("next_run_at"):
        parsed = fallback_once(text, tz)
    task = ScheduledTask(
        user_id=user.id,
        title=parsed["title"],
        instruction=parsed["instruction"],
        prompt=text,
        schedule_kind=parsed["schedule_kind"],
        schedule_label=parsed["schedule_label"],
        next_run_at=parsed["next_run_at"].astimezone(timezone.utc),
        status="active",
        connected_apps=await infer_apps(db, user, text),
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return serialize(task, tz)


async def list_tasks(db: AsyncSession, user: User, status: str | None = None) -> list[dict]:
    tz = user.timezone or "Asia/Kolkata"
    stmt = select(ScheduledTask).where(ScheduledTask.user_id == user.id).order_by(ScheduledTask.created_at.desc())
    if status:
        stmt = stmt.where(ScheduledTask.status == status)
    rows = (await db.execute(stmt)).scalars().all()
    return [serialize(r, tz) for r in rows]


async def get_owned(db: AsyncSession, user: User, task_id: uuid.UUID) -> ScheduledTask:
    task = await db.get(ScheduledTask, task_id)
    if task is None or task.user_id != user.id:
        raise KeyError("task")
    return task


async def set_status(db: AsyncSession, user: User, task_id: uuid.UUID, status: str) -> dict:
    if status not in {"active", "paused"}:
        raise ValueError("status must be active or paused")
    task = await get_owned(db, user, task_id)
    if task.status == "running":
        raise ValueError("Wait until the current run finishes.")
    if task.status == "completed" and status == "active" and task.schedule_kind == "once":
        raise ValueError("This one-time task already completed.")
    task.status = status
    await db.commit()
    return serialize(task, user.timezone or "Asia/Kolkata")


async def delete_task(db: AsyncSession, user: User, task_id: uuid.UUID) -> None:
    task = await get_owned(db, user, task_id)
    await db.delete(task)
    await db.commit()


async def infer_apps(db: AsyncSession, user: User, text: str) -> list[str]:
    conns = await user_connectors(db, user.id)
    names: list[str] = []
    for connector in mentioned_connectors(text, conns):
        if connector.enabled and connector.name not in names:
            names.append(connector.name)
    if names:
        return names[:6]
    lowered = text.lower()
    mailish = any(word in lowered for word in ("email", "mail", "inbox", "send"))
    for connector in conns:
        if not connector.enabled:
            continue
        labels = [connector.slug, (connector.alias or "").lower(), connector.name.lower()]
        if any(label and len(label) >= 3 and label in lowered for label in labels):
            names.append(connector.name)
        elif mailish and connector.provider in {"gmail", "outlook"}:
            names.append(connector.name)
    return names[:6]


async def apps_from_used(db: AsyncSession, user: User, used_tools: list[str] | None) -> list[str]:
    slugs: list[str] = []
    for name in used_tools or []:
        if "__" in name:
            slugs.append(name.split("__", 1)[0])
    conns = await user_connectors(db, user.id)
    by_slug = {c.slug: c.name for c in conns}
    out: list[str] = []
    for slug in slugs:
        name = by_slug.get(slug)
        if name and name not in out:
            out.append(name)
    return out


def advance(task: ScheduledTask) -> None:
    if task.schedule_kind == "once":
        task.status = "completed"
        task.next_run_at = None
        return
    step = timedelta(days=7 if task.schedule_kind == "weekly" else 1)
    nxt = task.next_run_at or datetime.now(timezone.utc)
    now = datetime.now(timezone.utc)
    while nxt <= now:
        nxt = nxt + step
    task.next_run_at = nxt
    task.status = "active"


async def execute_task(db: AsyncSession, user: User, task: ScheduledTask, *, from_scheduler: bool) -> dict:
    prev_status = task.status
    if prev_status == "running" and not from_scheduler:
        raise ValueError("This task is already running.")
    if prev_status != "running":
        task.status = "running"
        await db.commit()
    instruction = (
        "This is a scheduled run. Complete the work now using the user's connected tools. "
        "Do not create another scheduled task.\n\n"
        + (task.instruction or task.prompt)
    )
    sess = ChatSession(user_id=user.id, title=f"Task · {task.title}"[:200], state={"scheduled_task_id": str(task.id)})
    db.add(sess)
    await db.flush()
    request_id = random_token(12)
    try:
        result = await run_deep_agent(db, user, sess.id, request_id, [], instruction, auto_confirm=True)
        text = result.get("text") or "Done."
        used = result.get("used_tools") or []
        task.last_result = clip(text, 8000)
        task.last_error = ""
        task.last_run_at = datetime.now(timezone.utc)
        task.run_count = (task.run_count or 0) + 1
        apps = await apps_from_used(db, user, used)
        if apps:
            task.connected_apps = apps
        db.add(ChatMessage(session_id=sess.id, user_id=user.id, role="user", content=instruction, request_id=request_id))
        db.add(
            ChatMessage(
                session_id=sess.id,
                user_id=user.id,
                role="assistant",
                content=text,
                meta={"used_tools": used, "scheduled": True},
                request_id=request_id,
            )
        )
        if from_scheduler:
            advance(task)
        elif task.schedule_kind == "once":
            task.status = "completed"
            task.next_run_at = None
        elif prev_status == "paused":
            task.status = "paused"
        else:
            task.status = "active"
        await db.commit()
        return serialize(task, user.timezone or "Asia/Kolkata")
    except Exception as exc:  # noqa: BLE001
        log.exception("scheduled task %s failed", task.id)
        task.last_error = str(exc)[:800]
        task.last_run_at = datetime.now(timezone.utc)
        if from_scheduler:
            advance(task)
        elif prev_status == "paused":
            task.status = "paused"
        else:
            task.status = "active" if task.schedule_kind != "once" else "completed"
        await db.commit()
        raise


def local_next(task: ScheduledTask, tz: str):
    if not task.next_run_at:
        return None
    return as_local(task.next_run_at, tz)
