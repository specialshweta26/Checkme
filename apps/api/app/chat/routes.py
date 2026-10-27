from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

log = logging.getLogger(__name__)
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.attachments import summarize_one
from app.agent.confirm import read_confirm
from app.agent.deep import run_deep_agent
from app.agent.memory import seed_letta
from app.agents import service as agents_service
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
    attachments: list[dict] = []
    agent_id: uuid.UUID | None = None
    group_id: uuid.UUID | None = None


@router.get("/sessions")
async def sessions(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(ChatSession).where(ChatSession.user_id == user.id).order_by(ChatSession.updated_at.desc()).limit(30)
        )
    ).scalars().all()
    return {
        "sessions": [
            {
                "id": str(s.id),
                "title": s.title,
                "updated_at": s.updated_at.isoformat(),
                "agent_id": str(s.agent_id) if s.agent_id else None,
                "group_id": str(s.group_id) if s.group_id else None,
            }
            for s in rows
        ]
    }


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
        "session": {
            "id": str(sess.id),
            "title": sess.title,
            "agent_id": str(sess.agent_id) if sess.agent_id else None,
            "group_id": str(sess.group_id) if sess.group_id else None,
        },
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
async def chat(
    body: ChatIn,
    background: BackgroundTasks,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    if await rate_limited(f"chat:{user.id}", get_settings().chat_rate_limit_per_minute):
        raise HTTPException(429, "Too many messages. Wait a minute.")
    text = (body.message or "").strip()
    uploads = [item for item in (body.attachments or []) if isinstance(item, dict) and item.get("key")]
    if not text and not uploads:
        raise HTTPException(400, "Empty message")
    if not text:
        text = "Please review the attached files."
    agent_text = text
    if uploads:
        bits = []
        keys = []
        for item in uploads[:8]:
            keys.append(item.get("key"))
            name = item.get("filename") or "file"
            extracted = (item.get("extracted_text") or "").strip()
            bits.append(f"- {name} (attachment_key={item.get('key')})" + (f"\n{extracted[:3000]}" if extracted else ""))
        agent_text = (
            text
            + "\n\nThe user uploaded these files. Use attachment_keys when sending or replying to mail. "
            "Analyze them in detail; do not give a brief summary.\n"
            + "\n".join(bits)
        )

    try:
        workspace_agent, group, teammates = await _resolve_roster(db, user, body.agent_id, body.group_id)
    except agents_service.AgentError as exc:
        raise HTTPException(400, str(exc)) from exc

    mentions: dict = {}
    want_agent = None if group is not None else workspace_agent.id
    want_group = group.id if group is not None else None
    sess = None
    if body.session_id:
        found = await db.get(ChatSession, body.session_id)
        if found is None or found.user_id != user.id:
            raise HTTPException(404, "Unknown session")
        if agents_service.session_belongs(found, agent_id=want_agent, group_id=want_group):
            sess = found
    if sess is None:
        sess = ChatSession(
            user_id=user.id,
            title=text[:80],
            state={},
            agent_id=want_agent,
            group_id=want_group,
        )
        db.add(sess)
        await db.flush()

    if not user.letta_agent_id:
        background.add_task(seed_letta, user.id)

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
    if group is not None and teammates:
        from app.agents.collab import parse_mentions, pick_owner

        mentions = parse_mentions(text, teammates)
        owner = pick_owner(teammates, group, working, mentions)
        if owner is not None:
            workspace_agent = owner
            working["owner_id"] = str(owner.id)

    # #region agent log
    from app.debug_ndjson import dbg

    dbg("B", "chat/routes.py:chat", "chat request", {"query": text[:80], "has_session": bool(body.session_id)})
    # #endregion
    t0 = time.time()
    try:
        result = await run_deep_agent(
            db,
            user,
            sess.id,
            request_id,
            history,
            agent_text,
            confirms,
            workspace_agent=workspace_agent,
            group=group,
            teammates=teammates,
            mentions=mentions,
        )
        # #region agent log
        dbg(
            "C",
            "chat/routes.py:chat",
            "agent returned",
            {
                "ms": int((time.time() - t0) * 1000),
                "n_tools": len(result.get("used_tools") or []),
                "used_tools": (result.get("used_tools") or [])[:16],
            },
        )
        # #endregion
    except Exception as exc:  # noqa: BLE001
        log.exception("chat agent failed")
        # #region agent log
        dbg("C", "chat/routes.py:chat", "agent exception", {"error": str(exc)[:200], "ms": int((time.time() - t0) * 1000)})
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
            "mail_threads": result.get("mail_threads") or [],
            "browser": result.get("browser") or [],
            "computer": result.get("computer") or {},
            "connect_cards": result.get("connect_cards") or [],
            "speaker": result.get("speaker"),
            "agent_id": (result.get("speaker") or {}).get("id") if result.get("speaker") else (str(workspace_agent.id) if workspace_agent else None),
            "agent_name": (result.get("speaker") or {}).get("name"),
            "group_id": result.get("group_id"),
        }
        public_uploads = [
            {k: item.get(k) for k in ("key", "filename", "content_type", "kind", "download_url", "preview_url", "previewable", "size") if item.get(k) is not None}
            for item in uploads
        ]
        await _store(db, sess, user.id, request_id, text, result["text"], meta, user_meta={"uploads": public_uploads} if public_uploads else None)
        return {"session_id": str(sess.id), **result, "request_id": request_id, "uploads": public_uploads}
    except Exception as exc:  # noqa: BLE001
        log.exception("chat store failed")
        raise HTTPException(500, str(exc)[:400]) from exc


class SummarizeIn(BaseModel):
    message_id: str
    attachment_id: str
    connector_slug: str


@router.get("/thread")
async def chat_thread(
    slug: str = "",
    thread_id: str = "",
    message_id: str = "",
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.inbox import service as inbox_service

    try:
        return await inbox_service.open_full_thread(db, user, slug=slug, thread_id=thread_id, message_id=message_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, str(exc)[:300]) from exc


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


async def _resolve_roster(db, user, agent_id, group_id):
    chief = await agents_service.ensure_default_agent(db, user)
    if group_id:
        group, teammates = await agents_service.get_group(db, user, group_id)
        from app.agents.collab import pick_owner

        lead = pick_owner(teammates, group) or (teammates[0] if teammates else chief)
        return lead, group, teammates
    if agent_id:
        return await agents_service.get_agent(db, user, agent_id), None, []
    return chief, None, []


async def _store(db, sess, user_id, request_id, user_text, assistant_text, meta, user_meta=None) -> None:
    db.add(ChatMessage(session_id=sess.id, user_id=user_id, role="user", content=user_text, meta=user_meta or {}, request_id=request_id))
    db.add(ChatMessage(session_id=sess.id, user_id=user_id, role="assistant", content=assistant_text, meta=meta, request_id=request_id))
    if not sess.title or sess.title == "New chat":
        sess.title = user_text[:80]
    sess.updated_at = datetime.now(timezone.utc)
    await db.commit()
