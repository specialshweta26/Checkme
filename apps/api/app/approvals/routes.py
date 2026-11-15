"""/api/approvals: the one queue of everything waiting on you (docs/APPROVALS.md)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.approvals import service
from app.db import get_db
from app.deps import current_user
from app.jobs.service import JobError
from app.models import User

router = APIRouter(prefix="/approvals", tags=["approvals"])


class Decision(BaseModel):
    id: str = ""
    kind: str
    job_id: str
    token: str = ""
    approve: bool


class DecideIn(BaseModel):
    decisions: list[Decision] = Field(default_factory=list)


class AnswerIn(BaseModel):
    job_id: str
    text: str


class DismissIn(BaseModel):
    message_id: int
    token: str


@router.get("")
async def list_queue(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await service.queue(db, user)


@router.get("/count")
async def queue_count(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return {"count": await service.count(db, user)}


@router.post("/decide")
async def decide(body: DecideIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if not body.decisions:
        raise HTTPException(400, "Nothing to decide.")
    try:
        results = await service.decide(db, user, [d.model_dump() for d in body.decisions])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"results": results, "done": sum(1 for r in results if r["ok"])}


@router.post("/answer")
async def answer(body: AnswerIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if not body.text.strip():
        raise HTTPException(400, "Pick an answer.")
    try:
        return await service.answer(db, user, body.job_id, body.text)
    except LookupError as exc:
        raise HTTPException(404, "That task no longer exists.") from exc
    except JobError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/dismiss")
async def dismiss(body: DismissIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if not await service.dismiss_chat(db, user, body.message_id, body.token):
        raise HTTPException(404, "That approval is no longer waiting.")
    return {"ok": True}
