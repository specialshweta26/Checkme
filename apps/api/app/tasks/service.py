from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.deep import run_deep_agent
from app.agents.service import AgentError, ensure_default_agent, get_agent
from app.models import AgentJob, ChatMessage, ChatSession, ScheduledRun, ScheduledTask, User, WorkspaceAgent
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
        "chat_session_id": str(task.chat_session_id) if task.chat_session_id else None,
        "title": task.title,
        "instruction": task.instruction,
        "prompt": task.prompt,
        "schedule_kind": task.schedule_kind,
        "interval_minutes": task.interval_minutes,
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
    db: AsyncSession, user: User, prompt: str, *, require_time: bool = False, agent_id=None, chat_session_id=None
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
        interval_minutes=parsed.get("interval_minutes"),
        schedule_label=parsed["schedule_label"],
        next_run_at=parsed["next_run_at"].astimezone(timezone.utc),
        status="active",
        connected_apps=await infer_apps(db, user, text),
        chat_session_id=chat_session_id,
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
        if task.schedule_kind == "interval":
            nxt += timedelta(minutes=max(1, task.interval_minutes or 1))
        elif task.schedule_kind == "weekly":
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
    sess = await db.get(ChatSession, task.chat_session_id) if task.chat_session_id else None
    if sess is None or sess.user_id != user.id or sess.agent_id != agent.id:
        sess = ChatSession(
            user_id=user.id,
            title=f"Task · {task.title}"[:200],
            state={"scheduled_task_id": str(task.id)},
            agent_id=agent.id,
        )
        db.add(sess)
        await db.flush()
        task.chat_session_id = sess.id
    request_id = random_token(12)
    started = datetime.now(timezone.utc)
    trigger = "schedule" if from_scheduler else "manual"
    try:
        result = await run_deep_agent(
            db, user, sess.id, request_id, [], instruction, auto_confirm=True, workspace_agent=agent
        )
        text = result.get("text") or "Done."
        used = result.get("used_tools") or []
        held = await hold_for_approval(db, user, task, agent, sess, instruction, text, result.get("pending_confirms") or [])
        if held is not None:
            text = f"{text}\n\nI’ve prepared {held['pending_titles']}. Would you like me to go ahead? Reply naturally in this agent chat."
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
        record_run(
            db, task, agent, sess, trigger=trigger, started=started,
            status="needs_approval" if held else "succeeded", result=text,
            job_id=(held or {}).get("job", {}).get("id"),
        )  # fmt: skip
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
        task.last_error = str(exc)[:800] or type(exc).__name__
        task.last_result = f"I couldn’t complete “{task.title}” this time. {task.last_error}"[:8000]
        task.last_run_at = datetime.now(timezone.utc)
        if from_scheduler:
            advance(task, user.timezone or "Asia/Kolkata")
        elif prev_status == "paused":
            task.status = "paused"
        else:
            task.status = "active" if task.schedule_kind != "once" else "completed"
        # Keep failures visible in the same agent-scoped conversation as successful scheduled runs.
        if "sess" in locals():
            db.add(ChatMessage(session_id=sess.id, user_id=user.id, role="user", content=instruction, request_id=request_id))
            db.add(ChatMessage(
                session_id=sess.id,
                user_id=user.id,
                role="assistant",
                content=task.last_result,
                meta={"scheduled": True, "scheduled_failed": True, "agent_id": str(agent.id)},
                request_id=request_id,
            ))
            agent.unread = True
        record_run(
            db, task, locals().get("agent"), locals().get("sess"), trigger=trigger, started=started,
            status="failed", result=task.last_result, error=task.last_error,
        )  # fmt: skip
        await db.commit()
        raise


# ---------------------------------------------------------------- run history (Routines dashboard)

RUN_STATUSES = ("succeeded", "failed", "needs_approval")


def record_run(db, task, agent, sess, *, trigger: str, started: datetime, status: str, result: str = "", error: str = "", job_id=None) -> None:
    """Keep one row per run, and surface the result wherever the user looks: the agent is marked unread and the run's
    conversation moves to the top, so web and desktop open the same, newest conversation (app/chat/page.tsx)."""
    now = datetime.now(timezone.utc)
    db.add(
        ScheduledRun(
            user_id=task.user_id,
            task_id=task.id,
            agent_id=getattr(agent, "id", None),
            session_id=getattr(sess, "id", None),
            job_id=uuid.UUID(str(job_id)) if job_id else None,
            status=status if status in RUN_STATUSES else "failed",
            trigger=trigger,
            started_at=started,
            finished_at=now,
            result=clip(result or "", 4000),
            error=(error or "")[:2000],
        )
    )
    if sess is not None:
        sess.updated_at = now
    if agent is not None:
        agent.unread = True


async def backfill_runs(db: AsyncSession, user: User) -> None:
    """Tasks that ran before run history existed get one row from their last run, so the dashboard isn't empty."""
    from sqlalchemy import exists

    stmt = select(ScheduledTask).where(
        ScheduledTask.user_id == user.id,
        ScheduledTask.last_run_at.is_not(None),
        ~exists().where(ScheduledRun.task_id == ScheduledTask.id),
    )
    rows = (await db.execute(stmt)).scalars().all()
    for task in rows:
        db.add(
            ScheduledRun(
                user_id=user.id,
                task_id=task.id,
                agent_id=task.agent_id,
                session_id=task.chat_session_id,
                status="failed" if task.last_error else "succeeded",
                trigger="earlier",
                started_at=task.last_run_at,
                finished_at=task.last_run_at,
                result=clip(task.last_result or "", 4000),
                error=(task.last_error or "")[:2000],
            )
        )
    if rows:
        await db.commit()


def _outcome(run: ScheduledRun, job_status: str | None, job_error: str | None) -> tuple[str, str]:
    """A run parked for approval takes its job's outcome once the job ends."""
    if run.status == "needs_approval" and job_status:
        if job_status == "done":
            return "succeeded", run.error
        if job_status in ("failed", "cancelled"):
            return "failed", job_error or f"The approved work was {job_status}."
    return run.status, run.error


async def list_runs(
    db: AsyncSession, user: User, *, agent_id=None, task_id=None, status: str = "", days: int = 30, limit: int = 100
) -> list[dict]:
    await backfill_runs(db, user)
    tz = user.timezone or "Asia/Kolkata"
    since = datetime.now(timezone.utc) - timedelta(days=max(1, min(int(days or 30), 365)))
    stmt = (
        select(ScheduledRun, ScheduledTask.title, WorkspaceAgent.name, WorkspaceAgent.color, AgentJob.status, AgentJob.error)
        .join(ScheduledTask, ScheduledTask.id == ScheduledRun.task_id)
        .outerjoin(WorkspaceAgent, WorkspaceAgent.id == ScheduledRun.agent_id)
        .outerjoin(AgentJob, AgentJob.id == ScheduledRun.job_id)
        .where(ScheduledRun.user_id == user.id, ScheduledRun.started_at >= since)
        .order_by(ScheduledRun.started_at.desc())
    )
    if agent_id:
        stmt = stmt.where(ScheduledRun.agent_id == agent_id)
    if task_id:
        stmt = stmt.where(ScheduledRun.task_id == task_id)
    out = []
    for run, title, name, color, job_status, job_error in (await db.execute(stmt.limit(2000))).all():
        state, error = _outcome(run, job_status, job_error)
        if status and state != status:
            continue
        out.append(
            {
                "id": str(run.id),
                "task_id": str(run.task_id),
                "task_title": title,
                "agent_id": str(run.agent_id) if run.agent_id else None,
                "agent_name": name or "",
                "agent_color": color or "",
                "session_id": str(run.session_id) if run.session_id else None,
                "job_id": str(run.job_id) if run.job_id else None,
                "status": state,
                "trigger": run.trigger,
                "started_at": run.started_at.isoformat(),
                "started_label": format_local(run.started_at, tz),
                "duration_ms": int((run.finished_at - run.started_at).total_seconds() * 1000) if run.finished_at else None,
                "result": run.result,
                "error": error,
            }
        )
        if len(out) >= max(1, min(int(limit or 100), 500)):
            break
    return out


async def summary(db: AsyncSession, user: User, *, agent_id=None, days: int = 30) -> dict:
    """Counts for the Routines dashboard: runs by outcome, routines by state, and the same per agent."""
    runs = await list_runs(db, user, agent_id=agent_id, days=days, limit=500)
    tasks = await list_tasks(db, user, agent_id=agent_id)
    totals = {k: 0 for k in RUN_STATUSES}
    per: dict[str, dict] = {}
    for t in tasks:
        row = per.setdefault(t["agent_id"] or "", {"agent_id": t["agent_id"], "agent_name": t["agent_name"], "agent_color": t["agent_color"], "routines": 0, "active": 0, **{k: 0 for k in RUN_STATUSES}})
        row["routines"] += 1
        row["active"] += t["status"] in ("active", "running")
    for r in runs:
        totals[r["status"]] = totals.get(r["status"], 0) + 1
        row = per.setdefault(r["agent_id"] or "", {"agent_id": r["agent_id"], "agent_name": r["agent_name"], "agent_color": r["agent_color"], "routines": 0, "active": 0, **{k: 0 for k in RUN_STATUSES}})
        row[r["status"]] = row.get(r["status"], 0) + 1
    finished = totals["succeeded"] + totals["failed"]
    return {
        "days": days,
        "runs": len(runs),
        **totals,
        "success_rate": round(100 * totals["succeeded"] / finished) if finished else None,
        "routines": len(tasks),
        "active": sum(1 for t in tasks if t["status"] in ("active", "running")),
        "paused": sum(1 for t in tasks if t["status"] == "paused"),
        "last_failure": next((r for r in runs if r["status"] == "failed"), None),
        "agents": sorted(per.values(), key=lambda a: (a["agent_name"] or "").lower()),
    }


def local_next(task: ScheduledTask, tz: str):
    if not task.next_run_at:
        return None
    return as_local(task.next_run_at, tz)
