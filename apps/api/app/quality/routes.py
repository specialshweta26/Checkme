from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import current_user
from app.models import User
from app.quality import service

router = APIRouter(prefix="/quality", tags=["quality"])


class RateIn(BaseModel):
    session_id: str
    request_id: str
    rating: int  # 1 good, -1 bad, 0 clear
    reasons: list[str] = []
    note: str = ""
    agent_id: str = ""


@router.post("/feedback")
async def rate_reply(body: RateIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Thumbs up or down on one reply (0 clears it). A thumbs-down can carry reasons and a note."""
    try:
        return await service.rate(db, user, **body.model_dump())
    except service.QualityError as exc:
        raise HTTPException(400, str(exc)) from None


@router.get("/feedback")
async def list_ratings(rating: int | None = None, agent_id: str = "", days: int = 30, limit: int = 100,
                       user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):  # fmt: skip
    return {"feedback": await service.list_feedback(db, user, rating=rating, agent_id=agent_id, days=days, limit=limit), "reasons": service.REASONS}


@router.get("/feedback/session/{session_id}")
async def session_ratings(session_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    try:
        return {"ratings": await service.ratings_for(db, user, session_id)}
    except service.QualityError as exc:
        raise HTTPException(400, str(exc)) from None


@router.get("/summary")
async def quality_summary(agent_id: str = "", days: int = 30, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """How agents are doing: ratings, routine outcomes and draft edits."""
    return await service.summary(db, user, agent_id=agent_id, days=days)


@router.get("/export")
async def export(ids: str = "", days: int = 90, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Thumbs-downs as agent test-case stubs (JSON) for evals/from_feedback.py."""
    try:
        cases = await service.export_cases(db, user, ids=[i for i in ids.split(",") if i], days=days)
    except service.QualityError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"cases": cases}
