"""Per-agent budgets and limits (docs/BUDGETS.md)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.budgets import service
from app.db import get_db
from app.deps import current_user
from app.models import User, WorkspaceAgent

router = APIRouter(prefix="/budgets", tags=["budgets"])


class LimitsIn(BaseModel):
    daily_usd: float | None = None
    monthly_usd: float | None = None
    daily_tokens: int | None = None
    task_minutes: int | None = None


async def _agent(db: AsyncSession, user: User, agent_id: uuid.UUID) -> WorkspaceAgent:
    agent = await db.get(WorkspaceAgent, agent_id)
    if agent is None or agent.user_id != user.id:
        raise HTTPException(status_code=404, detail="Unknown agent")
    return agent


@router.get("")
async def all_agents(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(WorkspaceAgent).where(WorkspaceAgent.user_id == user.id, WorkspaceAgent.archived.is_(False)))).scalars().all()
    return {"agents": [{"agent_id": str(a.id), "name": a.name, **(await service.status(db, a))} for a in rows]}


@router.get("/{agent_id}")
async def one(agent_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    agent = await _agent(db, user, agent_id)
    return {"agent_id": str(agent.id), **(await service.status(db, agent, fresh=True))}


@router.put("/{agent_id}")
async def set_limits(agent_id: uuid.UUID, body: LimitsIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    agent = await _agent(db, user, agent_id)
    try:
        agent.limits = service.clean_limits(body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    service.forget(agent.id)
    return {"agent_id": str(agent.id), **(await service.status(db, agent, fresh=True))}
