from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import service
from app.db import get_db
from app.deps import current_user
from app.models import User

router = APIRouter(prefix="/agents", tags=["agents"])


class AgentIn(BaseModel):
    name: str
    title: str = ""
    instructions: str = ""
    focus: str | None = None
    color: str | None = None


class AgentPatch(BaseModel):
    name: str | None = None
    title: str | None = None
    instructions: str | None = None
    focus: str | None = None
    color: str | None = None
    pinned: bool | None = None
    archived: bool | None = None
    unread: bool | None = None


class MoveIn(BaseModel):
    group_id: uuid.UUID


class ConnectorAgentsIn(BaseModel):
    agent_ids: list[uuid.UUID] = []


class GroupIn(BaseModel):
    name: str
    member_ids: list[uuid.UUID]
    outcome: str = ""
    owner_id: uuid.UUID | None = None


class GroupPatch(BaseModel):
    name: str | None = None
    member_ids: list[uuid.UUID] | None = None
    outcome: str | None = None
    owner_id: uuid.UUID | None = None


def _err(exc: Exception) -> HTTPException:
    if isinstance(exc, service.AgentError):
        return HTTPException(400, str(exc))
    return HTTPException(400, str(exc)[:300])


@router.get("")
async def list_agents(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await service.list_workspace(db, user)


@router.post("")
async def create_agent(body: AgentIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    try:
        row = await service.create_agent(db, user, body.model_dump())
    except service.AgentError as exc:
        raise _err(exc) from exc
    return service.serialize_agent(row)


@router.patch("/{agent_id}")
async def patch_agent(
    agent_id: uuid.UUID,
    body: AgentPatch,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        row = await service.update_agent(db, user, agent_id, body.model_dump())
    except service.AgentError as exc:
        raise _err(exc) from exc
    return service.serialize_agent(row)


@router.post("/{agent_id}/duplicate")
async def duplicate_agent(agent_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    try:
        row = await service.duplicate_agent(db, user, agent_id)
    except service.AgentError as exc:
        raise _err(exc) from exc
    return service.serialize_agent(row)


@router.post("/{agent_id}/clear")
async def clear_agent_chat(agent_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    try:
        await service.clear_conversation(db, user, agent_id)
    except service.AgentError as exc:
        raise _err(exc) from exc
    return {"ok": True}


@router.post("/{agent_id}/move")
async def move_agent(
    agent_id: uuid.UUID,
    body: MoveIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        await service.add_agent_to_group(db, user, agent_id, body.group_id)
    except service.AgentError as exc:
        raise _err(exc) from exc
    return {"ok": True}


@router.put("/connector-assignments/{connector_id}")
async def assign_connector_agents(
    connector_id: uuid.UUID,
    body: ConnectorAgentsIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await service.set_connector_agents(db, user, connector_id, [str(i) for i in body.agent_ids])
    except service.AgentError as exc:
        raise _err(exc) from exc


@router.delete("/{agent_id}")
async def remove_agent(agent_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    try:
        await service.delete_agent(db, user, agent_id)
    except service.AgentError as exc:
        raise _err(exc) from exc
    return {"ok": True}


@router.post("/groups")
async def create_group(body: GroupIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    try:
        row, members = await service.create_group(
            db,
            user,
            {
                "name": body.name,
                "member_ids": [str(i) for i in body.member_ids],
                "outcome": body.outcome,
                "owner_id": str(body.owner_id) if body.owner_id else None,
            },
        )
    except service.AgentError as exc:
        raise _err(exc) from exc
    return service.serialize_group(row, members)


@router.patch("/groups/{group_id}")
async def patch_group(
    group_id: uuid.UUID,
    body: GroupPatch,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    payload = body.model_dump()
    if payload.get("member_ids") is not None:
        payload["member_ids"] = [str(i) for i in payload["member_ids"]]
    try:
        row, members = await service.update_group(db, user, group_id, payload)
    except service.AgentError as exc:
        raise _err(exc) from exc
    return service.serialize_group(row, members)


@router.delete("/groups/{group_id}")
async def remove_group(group_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    try:
        await service.delete_group(db, user, group_id)
    except service.AgentError as exc:
        raise _err(exc) from exc
    return {"ok": True}
