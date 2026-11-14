from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import current_user
from app.models import User
from app.tasks.service import create_from_prompt, delete_task, execute_task, get_owned, list_runs, list_tasks, set_status, summary

router = APIRouter(prefix="/tasks", tags=["tasks"])


class CreateTaskIn(BaseModel):
    prompt: str
    agent_id: uuid.UUID | None = None


class PatchTaskIn(BaseModel):
    status: str | None = None


@router.get("")
async def list_scheduled(
    status: str | None = None,
    agent_id: uuid.UUID | None = None,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        return {"tasks": await list_tasks(db, user, status=status, agent_id=agent_id)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/runs")
async def scheduled_runs(
    agent_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    status: str = "",
    days: int = 30,
    limit: int = 100,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Run history, newest first. status: succeeded | failed | needs_approval."""
    return {"runs": await list_runs(db, user, agent_id=agent_id, task_id=task_id, status=status, days=days, limit=limit)}


@router.get("/summary")
async def scheduled_summary(
    agent_id: uuid.UUID | None = None, days: int = 30, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """The Routines dashboard: runs by outcome over ``days``, routines by state, per agent."""
    return await summary(db, user, agent_id=agent_id, days=days)


@router.post("")
async def create_scheduled(body: CreateTaskIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    try:
        task = await create_from_prompt(db, user, body.prompt, agent_id=body.agent_id)
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
