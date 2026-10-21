from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import current_user
from app.inbox import service
from app.models import User

router = APIRouter(prefix="/inbox", tags=["inbox"])


class DraftIn(BaseModel):
    draft: str = ""


class RephraseIn(BaseModel):
    tone: str = "professional"
    instruction: str = ""
    draft: str = ""


class BatchIn(BaseModel):
    action: str
    confirm: bool = False
    item_ids: list[str] = []


@router.get("/badge")
async def inbox_badge(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    try:
        return await service.badge(db, user)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, str(exc)[:300]) from exc


@router.post("/seen")
async def mark_inbox_seen(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    try:
        return await service.mark_alerts_seen(db, user)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, str(exc)[:300]) from exc


@router.get("")
async def list_inbox(live: bool = False, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    try:
        return await service.list_inbox(db, user, live=live, mark_seen=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, str(exc)[:300]) from exc


@router.post("/batch")
async def inbox_batch(body: BatchIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    try:
        return await service.batch_mailbox(db, user, body.action, confirm=body.confirm, item_ids=body.item_ids)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, str(exc)[:300]) from exc


@router.patch("/{item_id}")
async def patch_draft(
    item_id: uuid.UUID,
    body: DraftIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await service.save_draft(db, user, item_id, body.draft)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/{item_id}/rephrase")
async def rephrase_draft(
    item_id: uuid.UUID,
    body: RephraseIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.draft.strip():
        try:
            await service.save_draft(db, user, item_id, body.draft)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
    try:
        return await service.rephrase(db, user, item_id, tone=body.tone, instruction=body.instruction)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, str(exc)[:300]) from exc


@router.post("/{item_id}/send")
async def send_draft(
    item_id: uuid.UUID,
    body: DraftIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await service.send_reply(db, user, item_id, body.draft or None)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, str(exc)[:300]) from exc


@router.post("/{item_id}/delete")
async def delete_mail(
    item_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await service.delete_item(db, user, item_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, str(exc)[:300]) from exc
