from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.deep import run_deep_agent
from app.agents.service import AgentError, ensure_default_agent, get_agent
from app.models import ChatMessage, ChatSession, ScheduledTask, User, WorkspaceAgent
from app.routing.describe import mentioned_connectors
from app.routing.select import user_connectors
from app.security import random_token
from app.tasks.parse import fallback_once, parse_schedule
from connector_core.calendar import as_local, format_local
from connector_core.text import clip

log = logging.getLogger(__name__)


def serialize(task: ScheduledTask, tz: str, *, agent_name: str = "", agent_color: str = "") -> dict:
    return {
        "id": str(task.id),
        "agent_id": str(task.agent_id) if task.agent_id else None,
        "agent_name": agent_name or "",
        "agent_color": agent_color or "",
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


async def resolve_task_agent(db: AsyncSession, user: User, agent_id):
    if agent_id:
        try:
            uid = agent_id if isinstance(agent_id, uuid.UUID) else uuid.UUID(str(agent_id))
        except (TypeError, ValueError) as exc:
            raise ValueError("Unknown agent") from exc
        try:
            return await get_agent(db, user, uid)
        except AgentError as exc:
            raise ValueError(str(exc)) from exc
    return await ensure_default_agent(db, user)


async def agent_for_task(db: AsyncSession, user: User, task: ScheduledTask):
    if task.agent_id:
        row = await db.get(WorkspaceAgent, task.agent_id)
        if row is not None and row.user_id == user.id:
            return row
    chief = await ensure_default_agent(db, user)
    task.agent_id = chief.id
    return chief


async def create_from_prompt(
    db: AsyncSession, user: User, prompt: str, *, require_time: bool = False, agent_id=None
) -> dict | None:
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
    agent = await resolve_task_agent(db, user, agent_id)
    task = ScheduledTask(
        user_id=user.id,
        agent_id=agent.id,
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
    return serialize(task, tz, agent_name=agent.name or "", agent_color=getattr(agent, "color", "") or "")


async def list_tasks(
    db: AsyncSession, user: User, status: str | None = None, agent_id: uuid.UUID | None = None
) -> list[dict]:
    tz = user.timezone or "Asia/Kolkata"
    if agent_id is not None:
        await resolve_task_agent(db, user, agent_id)
    stmt = (
        select(ScheduledTask, WorkspaceAgent.name, WorkspaceAgent.color)
        .outerjoin(WorkspaceAgent, WorkspaceAgent.id == ScheduledTask.agent_id)
        .where(ScheduledTask.user_id == user.id)
        .order_by(ScheduledTask.created_at.desc())
    )
    if status:
        stmt = stmt.where(ScheduledTask.status == status)
    if agent_id is not None:
        stmt = stmt.where(ScheduledTask.agent_id == agent_id)
    rows = (await db.execute(stmt)).all()
    return [serialize(task, tz, agent_name=name or "", agent_color=color or "") for task, name, color in rows]


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
    name, color = await _agent_labels(db, task)
    return serialize(task, user.timezone or "Asia/Kolkata", agent_name=name, agent_color=color)


async def _agent_labels(db: AsyncSession, task: ScheduledTask) -> tuple[str, str]:
    if not task.agent_id:
        return "", ""
    row = await db.get(WorkspaceAgent, task.agent_id)
    if row is None:
        return "", ""
    return row.name or "", row.color or ""


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


def advance(task: ScheduledTask, tz: str = "UTC") -> None:
    if task.schedule_kind == "once":
        task.status = "completed"
        task.next_run_at = None
        return
    # Advance in the user's timezone so recurring work stays at its chosen
    # wall-clock time across daylight-saving changes.
    nxt = as_local(task.next_run_at or datetime.now(timezone.utc), tz)
    now = as_local(datetime.now(timezone.utc), tz)
    while nxt <= now:
        if task.schedule_kind == "weekly":
            nxt += timedelta(days=7)
        else:
            nxt += timedelta(days=1)
            if task.schedule_kind == "weekdays":
                while nxt.weekday() >= 5:
                    nxt += timedelta(days=1)
    task.next_run_at = nxt.astimezone(timezone.utc)
    task.status = "active"


async def hold_for_approval(db, user, task, agent, sess, instruction: str, text: str, pending: list) -> dict | None:
    """A scheduled run that hit an approval card (git push / PR / merge are never auto-confirmed)
    becomes a background job parked in waiting_approval, so the user approves it on the job timeline
    in the task's chat and the run continues from there. Returns None when nothing is pending."""
    from app.jobs import service as jobs

    cards = [p for p in pending if isinstance(p, dict) and p.get("token")]
    if not cards:
        return None
    job = jobs.new_job(
        user.id,
        agent=agent,
        prompt=task.instruction or task.prompt or task.title,
        title=f"Scheduled · {task.title}",
        role="coder",
        kind="scheduled",
        session_id=sess.id,
        scheduled_task_id=task.id,
        history=[],
    )
    job.status = "running"
    db.add(job)
    for ev in jobs.apply_outcome(job, instruction, {"text": text, "pending_confirms": cards}):
        db.add(ev)
    agent.unread = True
    return {"job": jobs.serialize_job(job), "pending_titles": ", ".join(str(c.get("title") or "an action") for c in cards)}


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
    agent = await agent_for_task(db, user, task)
    sess = ChatSession(
        user_id=user.id,
        title=f"Task · {task.title}"[:200],
        state={"scheduled_task_id": str(task.id)},
        agent_id=agent.id,
    )
    db.add(sess)
    await db.flush()
    request_id = random_token(12)
    try:
        result = await run_deep_agent(
            db, user, sess.id, request_id, [], instruction, auto_confirm=True, workspace_agent=agent
        )
        text = result.get("text") or "Done."
        used = result.get("used_tools") or []
        held = await hold_for_approval(db, user, task, agent, sess, instruction, text, result.get("pending_confirms") or [])
        if held is not None:
            text = f"{text}\n\nWaiting for your approval: {held['pending_titles']}. Approve it in the task's chat."
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
                meta={"used_tools": used, "scheduled": True, **({"job": held["job"]} if held else {})},
                request_id=request_id,
            )
        )
        if from_scheduler:
            advance(task, user.timezone or "Asia/Kolkata")
        elif task.schedule_kind == "once":
            task.status = "completed"
            task.next_run_at = None
        elif prev_status == "paused":
            task.status = "paused"
        else:
            task.status = "active"
        await db.commit()
        return serialize(
            task,
            user.timezone or "Asia/Kolkata",
            agent_name=getattr(agent, "name", "") or "",
            agent_color=getattr(agent, "color", "") or "",
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("scheduled task %s failed", task.id)
        task.last_error = str(exc)[:800]
        task.last_run_at = datetime.now(timezone.utc)
        if from_scheduler:
            advance(task, user.timezone or "Asia/Kolkata")
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
