from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.automations import service
from app.db import get_db
from app.deps import current_user
from app.jobs.worker import wake
from app.models import User

router = APIRouter(prefix="/automations", tags=["automations"])
webhook_router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class WebhookAutomationIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    trigger: str = Field(min_length=1, max_length=160, description="Exact event type, such as issues or pull_request")
    action: str = Field(min_length=1, max_length=8000)
    condition: str = Field(default="", max_length=4000)
    agent_id: uuid.UUID | None = None
    requires_approval: bool = True


class AutomationPatch(BaseModel):
    enabled: bool


@router.get("")
async def list_automations(
    agent_id: uuid.UUID | None = None,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    return {"automations": await service.list_automations(db, user, agent_id)}


@router.post("/webhooks")
async def create_webhook(
    body: WebhookAutomationIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await service.create_webhook(
            db,
            user,
            name=body.name,
            trigger=body.trigger,
            action=body.action,
            condition=body.condition,
            agent_id=body.agent_id,
            requires_approval=body.requires_approval,
        )
    except (ValueError, LookupError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.patch("/{automation_id}")
async def patch_automation(
    automation_id: int,
    body: AutomationPatch,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await service.set_enabled(db, user, automation_id, body.enabled)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/{automation_id}")
async def delete_automation(
    automation_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        await service.delete(db, user, automation_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True}


@webhook_router.post("/{automation_id}")
async def receive_webhook(
    automation_id: int,
    request: Request,
    secret: str = Header(default="", alias="X-Checkme-Secret"),
    user_event_id: str = Header(default="", alias="Idempotency-Key"),
    github_delivery: str = Header(default="", alias="X-GitHub-Delivery"),
    db: AsyncSession = Depends(get_db),
):
    raw = await request.body()
    if len(raw) > service.MAX_EVENT_BYTES:
        raise HTTPException(413, f"Webhook payload exceeds {service.MAX_EVENT_BYTES} bytes.")
    try:
        payload = json.loads(raw or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "Webhook body must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "Webhook body must be a JSON object.")
    event_name = (
        request.headers.get("X-Event-Type")
        or request.headers.get("X-GitHub-Event")
        or str(payload.get("event") or "")
    ).strip()[:160]
    event_id = (user_event_id or github_delivery or str(payload.get("event_id") or "").strip())[:255]
    if not event_id:
        raise HTTPException(400, "Provide Idempotency-Key, X-GitHub-Delivery, or a JSON event_id.")
    try:
        result = await service.accept_webhook(
            db,
            automation_id,
            secret=secret,
            event_name=event_name,
            event_id=event_id,
            payload=payload,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(413, str(exc)) from exc
    if result.get("accepted") and not result.get("duplicate") and not result.get("ignored"):
        wake()
    return JSONResponse(result, status_code=200 if result.get("duplicate") or result.get("ignored") else 202)
