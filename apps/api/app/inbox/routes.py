from __future__ import annotations

import uuid

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal, get_db
from app.deps import current_user
from app.inbox import service
from app.models import User

log = logging.getLogger(__name__)
router = APIRouter(prefix="/inbox", tags=["inbox"])


class DraftIn(BaseModel):
    draft: str = ""
    attachment_keys: list[str] = []


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


@router.get("/events")
async def inbox_events(user: User = Depends(current_user)):
    user_id = user.id

    async def gen():
        last = None
        ticks = 0
        while True:
            try:
                async with SessionLocal() as db:
                    current = await db.get(User, user_id)
                    if current is None:
                        break
                    if ticks % 5 == 0:
                        await service.refresh_mailboxes(db, current, unread_only=False, classify=False)
                    badge = await service.badge(db, current)
                    inbox = await service.list_inbox(db, current, live=False, mark_seen=False)
                payload = {
                    "count": badge.get("count") or 0,
                    "pending": badge.get("pending") or 0,
                    "items": badge.get("items") or [],
                    "inbox": inbox.get("items") or [],
                    "connected": inbox.get("connected", True),
                }
                stamp = (
                    payload["count"],
                    tuple(row.get("id") for row in payload["items"][:20]),
                    tuple((row.get("id"), row.get("classification"), row.get("draft")) for row in payload["inbox"][:40]),
                )
                if stamp != last:
                    yield f"event: inbox\ndata: {json.dumps(payload)}\n\n"
                    last = stamp
                else:
                    yield ": ping\n\n"
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                log.exception("inbox stream failed")
                yield "event: error\ndata: {}\n\n"
            ticks += 1
            await asyncio.sleep(2)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("")
async def list_inbox(live: bool = False, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    try:
        return await service.list_inbox(db, user, live=live, mark_seen=False)
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
        return await service.send_reply(db, user, item_id, body.draft or None, body.attachment_keys)
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
