from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import current_user
from app.jobs import service
from app.jobs.worker import wake
from app.models import AgentJob, User

router = APIRouter(prefix="/jobs", tags=["jobs"])


class TokenIn(BaseModel):
    token: str


class AnswerIn(BaseModel):
    text: str
    action_title: str | None = None
    token: str | None = None


async def _owned(db, user, job_id) -> AgentJob:
    try:
        return await service.get_job(db, user, job_id)
    except LookupError as exc:
        raise HTTPException(404, "Unknown job") from exc


@router.get("")
async def list_jobs(active: bool = False, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    q = select(AgentJob).where(AgentJob.user_id == user.id, AgentJob.parent_id.is_(None))
    if active:
        q = q.where(AgentJob.status.in_(service.ACTIVE))
    rows = (await db.execute(q.order_by(AgentJob.created_at.desc()).limit(30))).scalars().all()
    return {"jobs": [service.serialize_job(j) for j in rows]}


@router.get("/{job_id}")
async def get_job(job_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    job = await _owned(db, user, job_id)
    return service.serialize_job(job)


@router.get("/{job_id}/events")
async def job_events(job_id: str, after: int = 0, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Timeline for the whole job tree (the job's root and every delegated child)."""
    job = await _owned(db, user, job_id)
    return await service.tree(db, user, job.root_id or job.id, after)


async def _decide(job_id: str, body: TokenIn, user, db, approve: bool) -> dict:
    job = await _owned(db, user, job_id)
    try:
        events = service.approve(job, body.token) if approve else service.decline(job, body.token)
    except service.JobError as exc:
        raise HTTPException(400, str(exc)) from exc
    for ev in events:
        db.add(ev)
    await db.commit()
    wake()
    return service.serialize_job(job)


@router.post("/{job_id}/approve")
async def approve(job_id: str, body: TokenIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await _decide(job_id, body, user, db, True)


@router.post("/{job_id}/decline")
async def decline(job_id: str, body: TokenIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await _decide(job_id, body, user, db, False)


@router.post("/{job_id}/respond")
async def respond(job_id: str, body: AnswerIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Accept a plain-language response to the agent's conversational action request."""
    from app.models import ChatMessage

    job = await _owned(db, user, job_id)
    try:
        events = service.respond_to_approval(job, body.text, body.action_title, body.token)
    except service.JobError as exc:
        raise HTTPException(400, str(exc)) from exc
    for ev in events:
        db.add(ev)
    if job.session_id is not None:
        db.add(ChatMessage(
            session_id=job.session_id, user_id=user.id, role="user", content=body.text.strip()[:2000],
            meta={"job_answer": str(job.id)}, request_id=f"answer-{str(job.id)[:8]}",
        ))
    await db.commit()
    wake()
    return service.serialize_job(job)


@router.post("/{job_id}/answer")
async def answer(job_id: str, body: AnswerIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """The user picked one of the job's options (e.g. "Yes, merge it"). Shown in the chat as their message."""
    from app.models import ChatMessage

    job = await _owned(db, user, job_id)
    try:
        events = service.answer(job, body.text)
    except service.JobError as exc:
        raise HTTPException(400, str(exc)) from exc
    for ev in events:
        db.add(ev)
    if job.session_id is not None:
        db.add(
            ChatMessage(
                session_id=job.session_id, user_id=user.id, role="user", content=body.text.strip()[:2000],
                meta={"job_answer": str(job.id)}, request_id=f"answer-{str(job.id)[:8]}",
            )
        )
    await db.commit()
    wake()
    return service.serialize_job(job)


@router.post("/{job_id}/cancel")
async def cancel(job_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Cancel the job and everything under it in the tree (running steps stop at once, approvals are cleared)."""
    job = await _owned(db, user, job_id)
    await service.cancel_tree(db, job)
    await db.commit()
    return service.serialize_job(job)
