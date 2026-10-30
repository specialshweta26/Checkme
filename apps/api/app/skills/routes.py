from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
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
    name: str = ""


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
    return await service.start_teaching(user, body.agent_id, body.name)


@router.get("/teach/status")
async def teach_status(user: User = Depends(current_user)):
    return service.teaching_status(user)


@router.post("/teach/audio")
async def teach_audio(
    audio: UploadFile = File(...),
    offset_ms: int = Form(0),
    user: User = Depends(current_user),
):
    """One microphone clip recorded while teaching (webm/opus, audio/mp4 from iOS Safari, ...)."""
    limit = int(get_settings().teach_audio_max_bytes or 0) or 24 * 1024 * 1024
    data = await audio.read(limit + 1)
    try:
        return await service.add_narration(user, data, audio.content_type or "", audio.filename or "", offset_ms)
    except service.SkillError as exc:
        raise HTTPException(413 if "too large" in str(exc) else 400, str(exc)) from exc


@router.post("/teach/stop")
async def teach_stop(body: TeachStopIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    try:
        return await service.stop_teaching(db, user, body.name)
    except service.SkillError as vis_exc:
        raise _err(vis_exc) from vis_exc


@router.get("/tasks/states")
async def task_states(session_id: str = "", job_id: str = "", user: User = Depends(current_user)):
    """Which skill each task in a chat or job tree follows, its step, notes and artifacts (newest first)."""
    from app.skills import task_state

    for value in (session_id, job_id):
        if value:
            try:
                uuid.UUID(value)
            except ValueError:
                raise HTTPException(400, "Bad id") from None
    rows = await task_state.STORE.list_for(user.id, session_id=session_id or None, job_id=job_id or None)
    return {"states": [task_state.serialize(r) for r in rows]}


@router.get("/{skill_id}")
async def read_skill(skill_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """One skill in full: instructions, steps, narration transcript, notes and screenshot indexes."""
    row = await service.get_skill(db, user, str(skill_id))
    if row is None:
        raise HTTPException(404, "Unknown skill.")
    return service.serialize_skill(row, full=True)


@router.get("/{skill_id}/frames/{index}")
async def skill_frame(
    skill_id: uuid.UUID, index: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    data = await service.frame_bytes(db, user, str(skill_id), index)
    if not data:
        raise HTTPException(404, "No such screenshot.")
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@router.post("/{skill_id}/rewrite")
async def rewrite_skill(skill_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Re-run the AI write-up from the recorded steps (Retry)."""
    try:
        return await service.rewrite_skill(db, user, str(skill_id))
    except service.SkillError as exc:
        raise _err(exc) from exc


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
