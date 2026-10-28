from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import current_user
from app.models import User
from app.skills import service

router = APIRouter(prefix="/skills", tags=["skills"])


class SkillIn(BaseModel):
    name: str
    agent_id: uuid.UUID | None = None
    instruction: str = ""
    steps: list = []


class TeachIn(BaseModel):
    agent_id: uuid.UUID | None = None


class TeachStopIn(BaseModel):
    name: str = ""


def _err(exc: Exception) -> HTTPException:
    if isinstance(exc, service.SkillError):
        return HTTPException(400, str(exc))
    return HTTPException(400, str(exc)[:300])


@router.get("")
async def list_skills(
    agent_id: uuid.UUID | None = None,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    return {"skills": await service.list_skills(db, user, agent_id)}


@router.post("")
async def create_skill(body: SkillIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    try:
        row = await service.save_skill(
            db,
            user,
            name=body.name,
            agent_id=body.agent_id,
            kind="saved",
            steps=body.steps,
            instruction=body.instruction,
        )
    except service.SkillError as exc:
        raise _err(exc) from exc
    return service.serialize_skill(row)


@router.post("/teach/start")
async def teach_start(body: TeachIn, user: User = Depends(current_user)):
    return service.start_teaching(user, body.agent_id)


@router.post("/teach/stop")
async def teach_stop(body: TeachStopIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    try:
        return await service.stop_teaching(db, user, body.name)
    except service.SkillError as vis_exc:
        raise _err(vis_exc) from vis_exc


class SkillPatch(BaseModel):
    name: str | None = None
    instruction: str | None = None


@router.patch("/{skill_id}")
async def patch_skill(
    skill_id: uuid.UUID,
    body: SkillPatch,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        row = await service.update_skill(
            db,
            user,
            skill_id=str(skill_id),
            new_name=body.name or "",
            instruction=body.instruction,
        )
    except service.SkillError as vis_exc:
        raise _err(vis_exc) from vis_exc
    return service.serialize_skill(row)


@router.delete("/{skill_id}")
async def drop_skill(
    skill_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await service.delete_skill(db, user, skill_id=str(skill_id))
    except service.SkillError as vis_exc:
        raise _err(vis_exc) from vis_exc
