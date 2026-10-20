from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

log = logging.getLogger(__name__)
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.attachments import summarize_one
from app.agent.confirm import read_confirm
from app.agent.deep import run_deep_agent
from app.agent.memory import ensure_letta_agent
from app.cache import rate_limited
from app.config import get_settings
from app.connectors.runtime import RuntimeConnector
from app.db import get_db
from app.deps import current_user
from app.models import ChatMessage, ChatSession, Connector, User
from app.security import random_token

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatIn(BaseModel):
    message: str
    session_id: uuid.UUID | None = None
    confirmations: list[dict] = []


@router.get("/sessions")
async def sessions(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(ChatSession).where(ChatSession.user_id == user.id).order_by(ChatSession.updated_at.desc()).limit(30)
        )
    ).scalars().all()
    return {"sessions": [{"id": str(s.id), "title": s.title, "updated_at": s.updated_at.isoformat()} for s in rows]}


@router.get("/sessions/{session_id}")
async def session_messages(session_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    sess = await db.get(ChatSession, session_id)
    if sess is None or sess.user_id != user.id:
        raise HTTPException(404, "Not found")
    rows = (
        await db.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.id)
            .limit(200)
        )
    ).scalars().all()
    return {
        "session": {"id": str(sess.id), "title": sess.title},
        "messages": [{"role": m.role, "content": m.content, "meta": m.meta} for m in rows if m.role in ("user", "assistant")],
    }


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    sess = await db.get(ChatSession, session_id)
    if sess is None or sess.user_id != user.id:
        raise HTTPException(404, "Not found")
    await db.delete(sess)
    await db.commit()
    return {"ok": True}


@router.post("")
async def chat(body: ChatIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if await rate_limited(f"chat:{user.id}", get_settings().chat_rate_limit_per_minute):
        raise HTTPException(429, "Too many messages. Wait a minute.")
    text = (body.message or "").strip()
    if not text:
        raise HTTPException(400, "Empty message")

    if body.session_id:
        sess = await db.get(ChatSession, body.session_id)
        if sess is None or sess.user_id != user.id:
            raise HTTPException(404, "Unknown session")
    else:
        sess = ChatSession(user_id=user.id, title=text[:80], state={})
        db.add(sess)
        await db.flush()

    if not user.letta_agent_id:
        user.letta_agent_id = await ensure_letta_agent(user)
        await db.commit()

    request_id = random_token(12)
    history_rows = (
        await db.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == sess.id, ChatMessage.role.in_(("user", "assistant")))
            .order_by(ChatMessage.id.desc())
            .limit(get_settings().history_messages)
        )
    ).scalars().all()
    history = []
    prior_source = (sess.state or {}).get("last_source") if isinstance(sess.state, dict) else None
    for m in reversed(history_rows):
        content = m.content
        src = (m.meta or {}).get("source") if isinstance(m.meta, dict) else None
        if m.role == "assistant" and src:
            prior_source = src
            content = f"{content}\n\nTool evidence (cite source_tool if asked): {json.dumps(src, default=str)[:2000]}"
        history.append({"role": m.role, "content": content})

    confirms = []
    for item in body.confirmations:
        token = item.get("token")
        if not token:
            continue
        data = read_confirm(token)
        if data and data.get("kind") == "tool":
            confirms.append(data["payload"])

    working = dict(sess.state or {})
    if confirms:
        working["confirm_tools"] = confirms

    # #region agent log
    from app.debug_ndjson import dbg

    dbg("B", "chat/routes.py:chat", "chat request", {"query": text[:80], "has_session": bool(body.session_id)})
    # #endregion
    try:
        result = await run_deep_agent(db, user, sess.id, request_id, history, text, confirms)
    except Exception as exc:  # noqa: BLE001
        log.exception("chat agent failed")
        # #region agent log
        dbg("C", "chat/routes.py:chat", "agent exception", {"error": str(exc)[:200]})
        # #endregion
        raise HTTPException(502, str(exc)[:400]) from exc
    try:
        if result.get("source"):
            working["last_source"] = result["source"]
        sess.state = {
            **working,
            "last_tools": result.get("used_tools") or [],
        }
        meta = {
            "request_id": request_id,
            "route": result.get("route"),
            "used_tools": result.get("used_tools"),
            "pending_confirms": result.get("pending_confirms"),
            "attachments": result.get("attachments"),
            "source": result.get("source"),
        }
        await _store(db, sess, user.id, request_id, text, result["text"], meta)
        return {"session_id": str(sess.id), **result, "request_id": request_id}
    except Exception as exc:  # noqa: BLE001
        log.exception("chat store failed")
        raise HTTPException(500, str(exc)[:400]) from exc


class SummarizeIn(BaseModel):
    message_id: str
    attachment_id: str
    connector_slug: str


@router.post("/attachments/summarize")
async def summarize_attachment(body: SummarizeIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    row = (
        await db.execute(
            select(Connector).where(Connector.user_id == user.id, Connector.slug == body.connector_slug, Connector.enabled.is_(True))
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Connector not found")
    try:
        result = await summarize_one(user, RuntimeConnector(row), body.message_id, body.attachment_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, str(exc)[:300]) from exc
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


async def _store(db, sess, user_id, request_id, user_text, assistant_text, meta) -> None:
    db.add(ChatMessage(session_id=sess.id, user_id=user_id, role="user", content=user_text, request_id=request_id))
    db.add(ChatMessage(session_id=sess.id, user_id=user_id, role="assistant", content=assistant_text, meta=meta, request_id=request_id))
    if not sess.title or sess.title == "New chat":
        sess.title = user_text[:80]
    sess.updated_at = datetime.now(timezone.utc)
    await db.commit()
