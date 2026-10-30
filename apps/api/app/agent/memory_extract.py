"""Async extraction of persona deltas and standing facts after a turn."""

from __future__ import annotations

import json
import logging
import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.memory import (
    SCOPE_AGENT,
    SCOPE_SHARED,
    _SECRET_LANGUAGE,
    remember,
    upsert_core,
    read_core,
)
from app.llm import chat_complete
from app.models import ChatMessage, MemoryExtractJob, User, WorkspaceAgent

log = logging.getLogger(__name__)

_ACK = re.compile(
    r"^(ok|okay|thanks|thank you|thx|got it|cool|sure|yes|yep|yeah|no|nope|np|k)[\s!.]*$",
    re.I,
)
def wake() -> None:
    from app.agent.memory_worker import wake as _wake

    _wake()


async def enqueue_extract(
    db: AsyncSession,
    user_id,
    agent_id,
    session_id,
    request_id: str,
    *,
    user_text: str = "",
    assistant_text: str = "",
    peer: bool = False,
) -> MemoryExtractJob | None:
    if not agent_id or not (request_id or "").strip():
        return None
    existing = (
        await db.execute(
            select(MemoryExtractJob).where(
                MemoryExtractJob.request_id == str(request_id)[:64],
                MemoryExtractJob.agent_id == agent_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    row = MemoryExtractJob(
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        request_id=str(request_id)[:64],
        status="queued",
        payload={
            "user_text": (user_text or "")[:4000],
            "assistant_text": (assistant_text or "")[:4000],
            "peer": bool(peer),
        },
    )
    db.add(row)
    return row


async def enqueue_turn(
    db: AsyncSession,
    user_id,
    agent,
    session_id,
    request_id: str,
    *,
    user_text: str = "",
    assistant_text: str = "",
    thread: list | None = None,
) -> None:
    agent_id = getattr(agent, "id", None)
    await enqueue_extract(
        db,
        user_id,
        agent_id,
        session_id,
        request_id,
        user_text=user_text,
        assistant_text=assistant_text,
    )
    seen = {str(agent_id)} if agent_id else set()
    for item in thread or []:
        if not isinstance(item, dict):
            continue
        peer_id = item.get("agent_id")
        if not peer_id or str(peer_id) in seen:
            continue
        try:
            uid = peer_id if isinstance(peer_id, uuid.UUID) else uuid.UUID(str(peer_id))
        except (TypeError, ValueError):
            continue
        seen.add(str(uid))
        await enqueue_extract(
            db,
            user_id,
            uid,
            session_id,
            request_id,
            user_text=user_text,
            assistant_text=(item.get("text") or "")[:4000],
            peer=True,
        )
    wake()


def _skip_turn(user_text: str, assistant_text: str) -> bool:
    user = " ".join((user_text or "").split())
    if not user:
        return True
    if _ACK.match(user):
        return True
    if _SECRET_LANGUAGE.search(user) or _SECRET_LANGUAGE.search(assistant_text or ""):
        return True
    return False


async def _turn_texts(db: AsyncSession, job: MemoryExtractJob) -> tuple[str, str]:
    payload = job.payload or {}
    user_text = str(payload.get("user_text") or "")
    assistant_text = str(payload.get("assistant_text") or "")
    if job.request_id:
        rows = (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.user_id == job.user_id, ChatMessage.request_id == job.request_id)
                .order_by(ChatMessage.id)
            )
        ).scalars().all()
        users = []
        assistants = []
        for row in rows:
            meta = row.meta or {}
            if meta.get("in_progress"):
                continue
            peer = bool(meta.get("peer"))
            speaker = str(meta.get("agent_id") or "")
            if peer and speaker and speaker != str(job.agent_id):
                continue
            if row.role == "user":
                users.append(row.content or "")
            elif row.role == "assistant":
                assistants.append(row.content or "")
        if users:
            user_text = users[-1]
        if assistants:
            assistant_text = assistants[-1]
    return user_text, assistant_text


async def _extract_json(user_text: str, assistant_text: str) -> dict:
    prompt = (
        "Extract durable memory for a personal assistant. Return JSON only with keys: "
        "discard (bool), voice (string, how the user wants this agent to talk; empty if none), "
        "prefs (string, standing preference for this agent; empty if none), "
        "facts (array of {text, scope, supersede}). scope is agent or user_shared. "
        "user_shared only for identity facts every agent should know (name, city, timezone). "
        "Max 3 facts. Ignore acknowledgements, tool dumps, secrets, one-off tasks. "
        "supersede is optional existing fact text being replaced.\n"
        f"User: {user_text[:1500]}\nAssistant: {assistant_text[:1500]}"
    )
    try:
        resp = await chat_complete(
            [{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=500,
            temperature=0,
        )
        raw = (resp.choices[0].message.content or "").strip()
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        log.warning("memory extract llm failed: %s", exc)
        return {}


def _clean_fact(text: str) -> str:
    body = " ".join((text or "").split())
    if not body or _SECRET_LANGUAGE.search(body):
        return ""
    if _ACK.match(body):
        return ""
    return body[:800]


async def apply_extract(db: AsyncSession, user: User, agent, payload: dict, session_id=None) -> dict:
    if payload.get("discard") is True:
        return {"applied": False, "reason": "discard"}
    voice = _clean_fact(str(payload.get("voice") or ""))
    prefs = _clean_fact(str(payload.get("prefs") or ""))
    applied = {"voice": False, "prefs": False, "facts": 0}
    if voice:
        prior = await read_core(db, user, "voice", agent)
        if voice.lower() not in (prior or "").lower():
            merged = (prior.rstrip() + "\n" + voice).strip() if prior else voice
            await upsert_core(db, user.id, "voice", merged[:800], agent.id)
            applied["voice"] = True
    if prefs:
        prior = await read_core(db, user, "prefs", agent)
        if prefs.lower() not in (prior or "").lower():
            merged = (prior.rstrip() + "\n" + prefs).strip() if prior else prefs
            await upsert_core(db, user.id, "prefs", merged[:800], agent.id)
            applied["prefs"] = True
    for item in (payload.get("facts") or [])[:3]:
        if not isinstance(item, dict):
            continue
        body = _clean_fact(str(item.get("text") or ""))
        if not body:
            continue
        scope = SCOPE_SHARED if str(item.get("scope") or "") == SCOPE_SHARED else SCOPE_AGENT
        await remember(
            db,
            user.id,
            "archival",
            body,
            agent.id,
            scope=scope,
            source_session_id=session_id,
        )
        applied["facts"] += 1
    return {"applied": bool(applied["voice"] or applied["prefs"] or applied["facts"]), **applied}


async def process_job(db: AsyncSession, job: MemoryExtractJob) -> str:
    user = await db.get(User, job.user_id)
    agent = await db.get(WorkspaceAgent, job.agent_id)
    if user is None or agent is None:
        job.status = "failed"
        job.last_error = "missing user or agent"
        return job.status
    user_text, assistant_text = await _turn_texts(db, job)
    if _skip_turn(user_text, assistant_text):
        job.status = "done"
        job.last_error = ""
        return job.status
    payload = await _extract_json(user_text, assistant_text)
    if payload.get("discard") is True or not payload:
        job.status = "done"
        return job.status
    await apply_extract(db, user, agent, payload, job.session_id)
    job.status = "done"
    job.last_error = ""
    return job.status
