from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import current_user
from app.models import User
from app.tasks.service import create_from_prompt, delete_task, execute_task, get_owned, list_tasks, set_status

router = APIRouter(prefix="/tasks", tags=["tasks"])


class CreateTaskIn(BaseModel):
    prompt: str


class PatchTaskIn(BaseModel):
    status: str | None = None


@router.get("")
async def list_scheduled(status: str | None = None, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return {"tasks": await list_tasks(db, user, status=status)}


@router.post("")
async def create_scheduled(body: CreateTaskIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    try:
        task = await create_from_prompt(db, user, body.prompt)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return task


@router.patch("/{task_id}")
async def patch_scheduled(
    task_id: uuid.UUID, body: PatchTaskIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    if not body.status:
        raise HTTPException(400, "Nothing to update")
    try:
        return await set_status(db, user, task_id, body.status)
    except KeyError:
        raise HTTPException(404, "Task not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/{task_id}/run")
async def run_scheduled(task_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    try:
        task = await get_owned(db, user, task_id)
        return await execute_task(db, user, task, from_scheduler=False)
    except KeyError:
        raise HTTPException(404, "Task not found")
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.delete("/{task_id}")
async def remove_scheduled(task_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    try:
        await delete_task(db, user, task_id)
    except KeyError:
        raise HTTPException(404, "Task not found")
    return {"ok": True}
