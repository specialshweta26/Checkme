"""The Models page: a user's own model keys and the model their agents use. Keys go in, never come back out."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import current_user
from app.model_keys import service
from app.models import User

router = APIRouter(prefix="/models", tags=["models"])


class KeyIn(BaseModel):
    provider: str
    api_key: str
    label: str = ""
    base_url: str = ""


class ChoiceIn(BaseModel):
    key_id: str | None = None
    model: str = ""
    agent_id: str | None = None  # one agent's own model; none = the user's default


def _refused(exc: service.ModelKeyError) -> HTTPException:
    return HTTPException(400, str(exc))


@router.get("")
async def overview(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await service.overview(db, user)


@router.post("/keys")
async def add_key(body: KeyIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    try:
        return await service.add_key(db, user, body.provider, body.api_key, body.label, body.base_url)
    except service.ModelKeyError as exc:
        raise _refused(exc) from exc


@router.post("/keys/{key_id}/check")
async def recheck(key_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    try:
        return await service.recheck(db, user, key_id)
    except service.ModelKeyError as exc:
        raise _refused(exc) from exc


@router.delete("/keys/{key_id}")
async def remove_key(key_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    try:
        await service.remove_key(db, user, key_id)
    except service.ModelKeyError as exc:
        raise _refused(exc) from exc
    return await service.overview(db, user)


@router.put("/choices/{purpose}")
async def choose(purpose: str, body: ChoiceIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    try:
        return await service.choose(db, user, purpose, body.key_id, body.model, body.agent_id)
    except service.ModelKeyError as exc:
        raise _refused(exc) from exc
