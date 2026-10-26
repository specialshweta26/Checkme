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


@router.post("/{job_id}/cancel")
async def cancel(job_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Cancel the job and everything under it in the tree."""
    job = await _owned(db, user, job_id)
    rows = (
        await db.execute(select(AgentJob).where(AgentJob.user_id == user.id, AgentJob.root_id == (job.root_id or job.id)))
    ).scalars().all()
    by_parent: dict = {}
    for row in rows:
        by_parent.setdefault(row.parent_id, []).append(row)
    stack, hit = [job], []
    while stack:
        cur = stack.pop()
        hit.append(cur)
        stack.extend(by_parent.get(cur.id, []))
    for row in hit:
        for ev in service.cancel(row):
            db.add(ev)
    if job.parent_id is None:
        await service.post_to_chat(db, job, "Cancelled the background job.")
    else:
        await service.after_terminal(db, job)
    await db.commit()
    return service.serialize_job(job)
