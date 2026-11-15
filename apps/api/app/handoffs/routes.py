"""Handoff receipts and who-may-send rules (docs/HANDOFFS.md)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import current_user
from app.handoffs import service
from app.models import User, WorkspaceAgent

router = APIRouter(prefix="/handoffs", tags=["handoffs"])


class RuleIn(BaseModel):
    mode: str
    agents: list[str] = []


@router.get("")
async def list_handoffs(agent_id: uuid.UUID | None = None, limit: int = 50, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return {"messages": await service.history(db, user.id, agent_id, limit)}


@router.put("/rules/{agent_id}")
async def set_rule(agent_id: uuid.UUID, body: RuleIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    agent = await db.get(WorkspaceAgent, agent_id)
    if agent is None or agent.user_id != user.id:
        raise HTTPException(status_code=404, detail="Unknown agent")
    names = await service.agent_names(db, user.id)
    try:
        agent.accepts_from = service.clean_rule(body.mode, body.agents, set(names), str(agent.id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    return {"agent_id": str(agent.id), "accepts_from": service.rule(agent)}
