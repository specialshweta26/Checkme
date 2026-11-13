"""GET /api/today — the four columns on the Today page."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.service import purge_deleted_agent_history, serialize_agent
from app.db import get_db
from app.deps import current_user
from app.inbox.alerts import LABELS, open_alerts
from app.inbox.service import _connector_labels, mail_connectors, serialize as serialize_inbox
from app.jobs.service import serialize_job
from app.models import AgentJob, ChatMessage, ChatSession, User, WorkspaceAgent
from app.tasks.service import list_tasks
from app.today.board import assemble

router = APIRouter(prefix="/today", tags=["today"])


def _job_row(job: AgentJob, session_agent: dict) -> dict:
    data = serialize_job(job)
    if not data.get("agent_id") and job.session_id:
        agent_id = session_agent.get(job.session_id)
        if agent_id:
            data["agent_id"] = str(agent_id)
    data["scheduled_task_id"] = str(job.scheduled_task_id) if job.scheduled_task_id else None
    data["prompt"] = job.prompt or ""
    return data


def _reply_row(message: ChatMessage, agent_id) -> dict:
    meta = message.meta or {}
    return {
        "agent_id": str(agent_id) if agent_id else None,
        "session_id": str(message.session_id),
        "content": message.content or "",
        "created_at": message.created_at.isoformat() if message.created_at else None,
        "choices": meta.get("choices") if isinstance(meta.get("choices"), dict) else None,
        "pending": meta.get("pending_confirms") if isinstance(meta.get("pending_confirms"), list) else [],
    }


async def _latest_replies(db: AsyncSession, user: User) -> list[dict]:
    sessions = (
        await db.execute(
            select(ChatSession)
            .where(
                ChatSession.user_id == user.id,
                ChatSession.group_id.is_(None),
                ChatSession.agent_id.isnot(None),
            )
            .order_by(ChatSession.updated_at.desc())
            .limit(40)
        )
    ).scalars().all()
    if not sessions:
        return []
    ids = [s.id for s in sessions]
    latest = (
        select(ChatMessage.session_id, func.max(ChatMessage.id).label("mid"))
        .where(ChatMessage.session_id.in_(ids), ChatMessage.role == "assistant")
        .group_by(ChatMessage.session_id)
        .subquery()
    )
    messages = (
        await db.execute(select(ChatMessage).join(latest, ChatMessage.id == latest.c.mid))
    ).scalars().all()
    by_session = {s.id: s for s in sessions}
    newest: dict[str, dict] = {}
    for message in messages:
        sess = by_session.get(message.session_id)
        if sess is None or sess.agent_id is None:
            continue
        key = str(sess.agent_id)
        row = _reply_row(message, sess.agent_id)
        prev = newest.get(key)
        if prev is None or (row.get("created_at") or "") > (prev.get("created_at") or ""):
            newest[key] = row
    return list(newest.values())


async def _live_turns(db: AsyncSession, user: User) -> list[dict]:
    """Chat turns still writing a reply. A stale in_progress flag only counts while the turn is alive."""
    from app.agent.progress import turn_alive, valid_progress_id

    rows = (
        await db.execute(
            select(ChatMessage, ChatSession.agent_id)
            .join(ChatSession, ChatSession.id == ChatMessage.session_id)
            .where(ChatMessage.user_id == user.id, ChatMessage.meta.contains({"in_progress": True}))
            .order_by(ChatMessage.id.desc())
            .limit(12)
        )
    ).all()
    live = []
    for message, agent_id in rows:
        meta = message.meta or {}
        progress_id = str(meta.get("progress_id") or "")
        alive = None
        if progress_id and valid_progress_id(progress_id):
            alive = await turn_alive(user.id, progress_id)
        created = message.created_at
        if created is not None and created < datetime.now(timezone.utc) - timedelta(hours=6):
            continue
        if alive is not True and (created is None or created < datetime.now(timezone.utc) - timedelta(minutes=20)):
            continue
        live.append(
            {
                "id": str(message.id),
                "agent_id": str(agent_id) if agent_id else None,
                "session_id": str(message.session_id),
                "detail": "Working on a reply",
                "created_at": message.created_at.isoformat() if message.created_at else None,
            }
        )
    return live


@router.get("")
async def today(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    await purge_deleted_agent_history(db, user)
    agents = (
        await db.execute(
            select(WorkspaceAgent)
            .where(WorkspaceAgent.user_id == user.id)
            .order_by(WorkspaceAgent.created_at.asc())
        )
    ).scalars().all()
    jobs = (
        await db.execute(
            select(AgentJob)
            .where(AgentJob.user_id == user.id, AgentJob.parent_id.is_(None))
            .order_by(AgentJob.created_at.desc())
            .limit(60)
        )
    ).scalars().all()
    missing_sessions = [job.session_id for job in jobs if job.session_id and not job.agent_id]
    session_agent: dict = {}
    if missing_sessions:
        rows = (
            await db.execute(
                select(ChatSession.id, ChatSession.agent_id).where(ChatSession.id.in_(missing_sessions))
            )
        ).all()
        session_agent = {sid: aid for sid, aid in rows if aid}
    names, slugs = _connector_labels(await mail_connectors(db, user))
    alerts = await open_alerts(db, user.id)
    payload = assemble(
        now=datetime.now(timezone.utc),
        tz=user.timezone or "Asia/Kolkata",
        name=user.display_name or "",
        email=user.email or "",
        agents=[serialize_agent(row) for row in agents],
        jobs=[_job_row(job, session_agent) for job in jobs],
        tasks=await list_tasks(db, user),
        replies=await _latest_replies(db, user),
        alerts=[
            {
                **serialize_inbox(item, names.get(item.connector_id, ""), slugs.get(item.connector_id, "")),
                "alert_label": LABELS.get(item.alert_reason, "Needs a reply"),
            }
            for item in alerts
        ],
        live=await _live_turns(db, user),
    )
    return payload
