"""A separate browser per agent (docs/AGENT_BROWSERS.md)."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_browsers import service
from app.db import get_db
from app.deps import current_user
from app.models import AgentBrowserProfile, User, WorkspaceAgent

router = APIRouter(prefix="/agent-browsers", tags=["agent-browsers"])


class ModeIn(BaseModel):
    mode: Literal["shared", "own"]


async def _agent(db: AsyncSession, user: User, agent_id: uuid.UUID) -> WorkspaceAgent:
    agent = await db.get(WorkspaceAgent, agent_id)
    if agent is None or agent.user_id != user.id:
        raise HTTPException(status_code=404, detail="Unknown agent")
    return agent


async def _view(db: AsyncSession, agent: WorkspaceAgent) -> dict:
    s = service.settings(agent)
    row = await db.get(AgentBrowserProfile, agent.id)
    saved = row is not None and row.space == service.space_for(agent) and s["mode"] == "own"
    return {
        "agent_id": str(agent.id),
        "mode": s["mode"],
        "saved_cookies": int(row.cookie_count or 0) if saved else 0,
        "saved_at": row.updated_at.isoformat() if saved and row.updated_at else None,
    }


@router.get("/{agent_id}")
async def get_browser(agent_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await _view(db, await _agent(db, user, agent_id))


@router.put("/{agent_id}")
async def set_mode(agent_id: uuid.UUID, body: ModeIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    agent = await _agent(db, user, agent_id)
    agent.browser = {**service.settings(agent), "mode": body.mode}
    await db.commit()
    return await _view(db, agent)


@router.post("/{agent_id}/reset")
async def sign_out_everywhere(agent_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Start the agent on a new, empty browser profile and drop its saved cookies. The old profile is never used again."""
    agent = await _agent(db, user, agent_id)
    s = service.settings(agent)
    if s["mode"] != "own":
        raise HTTPException(status_code=400, detail="This agent uses the shared browser. Give it its own browser first.")
    agent.browser = {**s, "epoch": s["epoch"] + 1}
    row = await db.get(AgentBrowserProfile, agent.id)
    if row is not None:
        await db.delete(row)
    await db.commit()
    return await _view(db, agent)
