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
    SCOPE_USER,
    _SECRET_LANGUAGE,
    merge_block,
    reconcile_related,
    remember,
    retire_fact,
    upsert_core,
    read_core,
)
from app.llm import chat_complete, small_model
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
            "user_text": user_text or "",
            "assistant_text": assistant_text or "",
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
            assistant_text=item.get("text") or "",
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
    user_text, assistant_text, _pending = await _turn_parts(db, job)
    return user_text, assistant_text


async def _turn_parts(db: AsyncSession, job: MemoryExtractJob) -> tuple[str, str, bool]:
    """(user text, assistant text, whether the reply left an action waiting for the user's approval)."""
    payload = job.payload or {}
    user_text = str(payload.get("user_text") or "")
    assistant_text = str(payload.get("assistant_text") or "")
    pending = bool(payload.get("pending_confirms"))
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
                pending = bool(meta.get("pending_confirms"))
        if users:
            user_text = users[-1]
        if assistants:
            assistant_text = assistants[-1]
    return user_text, assistant_text, pending


KNOWN_FACTS = 12


async def known_facts(db: AsyncSession, user: User, agent, about: str) -> list[tuple[int, str]]:
    """The stored facts closest to this turn, as (id, text), so the extractor can say which one a new fact replaces."""
    if db is None:
        return []
    from app.agent.memory import _memory_agent, _ranked_facts

    try:
        owner = await _memory_agent(db, user, agent=agent)
        rows = await _ranked_facts(db, user.id, owner.id, about, limit=KNOWN_FACTS)
    except Exception:  # noqa: BLE001 - extraction still runs, it just can't replace anything
        log.info("memory extract: known facts unavailable", exc_info=True)
        return []
    return [(int(row.id), " ".join((row.text or "").split())) for row in rows if row.id is not None and row.text]


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _resolve_supersede(value, known: list[tuple[int, str]]) -> int | None:
    """Map the extractor's ``supersede`` (a known fact's id, or its text) to that fact's id. Anything else is ignored,
    so a fact the extractor wasn't shown can never be retired."""
    if value in (None, "", False) or not known:
        return None
    ids = {fid for fid, _ in known}
    raw = str(value).strip().lstrip("#")
    if raw.isdigit():
        return int(raw) if int(raw) in ids else None
    want = _norm(raw)
    if len(want) < 4:
        return None
    for fid, body in known:
        if _norm(body) == want:
            return fid
    for fid, body in known:
        have = _norm(body)
        if len(want) >= 8 and (want in have or have in want):
            return fid
    return None


async def _extract_json(
    user_text: str,
    assistant_text: str,
    known: list[tuple[int, str]] | None = None,
    saved_notes: list[str] | None = None,
) -> dict:
    listed = "\n".join(f"{fid}: {body}" for fid, body in (known or [])) or "(none)"
    notes = "\n".join(f"- {line}" for line in (saved_notes or [])) or "(none)"
    prompt = (
        "Extract durable memory for a personal assistant. Return JSON only with keys: "
        "discard (bool), voice (string, how the user wants to be answered; empty if none), "
        "prefs (string, a standing preference of the user; empty if none), "
        "facts (array of {text, about, predicate, said_by, scope, supersede}), retire (array of ids). "
        "text is one short fact. about is who the fact is about: omit it for the user themselves, otherwise "
        "{name, email, kind} with kind person, org, project or place (include the email only if it appears in the turn). "
        "predicate is a short snake_case key for the kind of fact (lives_in, works_at, role, prefers, phone, birthday, "
        "manages, deadline, status); two facts with the same about and predicate cannot both be true. "
        "said_by is user if the user stated it, assistant if it comes from the assistant's reply or a tool result. "
        "scope is user (default: a fact about the user's world, shared by all of their assistants) or agent (only "
        "about how this one assistant should work). "
        "Ignore acknowledgements, tool dumps, secrets, one-off tasks and facts already in the known list. The agent's "
        "saved notes are plain text: do not repeat what they say about the user themselves as facts, voice or prefs. "
        "But always return facts about other people, organisations and projects (with about and predicate), even if a "
        "saved note mentions them, so they are kept as structured facts. "
        "supersede is the id of the known fact that a new fact replaces because it changed or is no longer true "
        "(for example a new city, role, employer or email, for the user or for someone else); leave it out otherwise. "
        "retire lists the ids of known facts this turn shows are no longer true when no new fact here replaces "
        "them (for example because the saved notes already hold the new value).\n"
        f"Known facts (id: text):\n{listed}\n\n"
        f"Agent's saved notes (already kept; do not repeat):\n{notes}\n\n"
        f"User: {user_text}\nAssistant: {assistant_text}"
    )
    try:
        resp = await chat_complete(
            [{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=500,
            temperature=0,
            model=small_model(),
        )
        raw = (resp.choices[0].message.content or "").strip()
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        log.warning("memory extract llm failed: %s", exc)
        return {}


async def _subject_for(db: AsyncSession, user: User, about) -> int | None:
    """The entity a fact is about (None for the user themselves), found or created by email, then name."""
    if db is None or not isinstance(about, dict):
        return None
    name, email = str(about.get("name") or ""), str(about.get("email") or "")
    if not (name or email):
        return None
    try:
        from app.memory_engine.entities import resolve

        entity = await resolve(db, user.id, name=name, email=email, kind=str(about.get("kind") or "person"))
        return entity.id if entity is not None else None
    except Exception:  # noqa: BLE001 - the fact is still saved, just not linked
        log.info("memory entity resolve failed", exc_info=True)
        await db.rollback()
        return None


def _clean_fact(text: str) -> str:
    body = " ".join((text or "").split())
    if not body or _SECRET_LANGUAGE.search(body):
        return ""
    if _ACK.match(body):
        return ""
    return body


async def apply_extract(
    db: AsyncSession, user: User, agent, payload: dict, session_id=None, known: list[tuple[int, str]] | None = None
) -> dict:
    if payload.get("discard") is True:
        return {"applied": False, "reason": "discard"}
    voice = _clean_fact(str(payload.get("voice") or ""))
    prefs = _clean_fact(str(payload.get("prefs") or ""))
    applied = {"voice": False, "prefs": False, "facts": 0, "superseded": 0}
    for label, note in (("voice", voice), ("prefs", prefs)):
        if not note:
            continue
        prior = await read_core(db, user, label, agent)
        merged, changed = await merge_block(label, prior or "", note)
        if changed:
            await upsert_core(db, user.id, label, merged, agent.id)
            await reconcile_related(db, user, agent, label, note)
            applied[label] = True
    known = list(known or [])
    already = {_norm(body) for _fid, body in known}
    retired: set[int] = set()
    for item in payload.get("facts") or []:
        if not isinstance(item, dict):
            continue
        body = _clean_fact(str(item.get("text") or ""))
        if not body or _norm(body) in already:
            continue
        sid = _resolve_supersede(item.get("supersede"), known)
        if sid in retired:
            sid = None
        # Facts belong to the user (every one of their agents reads them) unless it is a note about this agent.
        scope = SCOPE_AGENT if str(item.get("scope") or "") == SCOPE_AGENT else SCOPE_USER
        subject_id = await _subject_for(db, user, item.get("about"))
        said_by = str(item.get("said_by") or "user").lower()
        await remember(
            db,
            user.id,
            "archival",
            body,
            agent.id,
            scope=scope,
            source_session_id=session_id,
            supersedes_id=sid,
            subject_entity_id=subject_id,
            predicate=str(item.get("predicate") or ""),
            trust="user_said" if said_by == "user" else "inferred",
        )
        already.add(_norm(body))
        applied["facts"] += 1
        if sid is not None:
            retired.add(sid)
            applied["superseded"] += 1
    for value in payload.get("retire") or []:
        rid = _resolve_supersede(value, known)
        if rid is None or rid in retired:
            continue
        if await retire_fact(db, user, agent, rid):
            retired.add(rid)
            applied["superseded"] += 1
    return {"applied": bool(applied["voice"] or applied["prefs"] or applied["facts"] or retired), **applied}


async def process_job(db: AsyncSession, job: MemoryExtractJob) -> str:
    user = await db.get(User, job.user_id)
    agent = await db.get(WorkspaceAgent, job.agent_id)
    if user is None or agent is None:
        job.status = "failed"
        job.last_error = "missing user or agent"
        return job.status
    user_text, assistant_text, pending = await _turn_parts(db, job)
    if _skip_turn(user_text, assistant_text):
        job.status = "done"
        job.last_error = ""
        return job.status
    known = await known_facts(db, user, agent, f"{user_text}\n{assistant_text}"[:2000])
    try:
        # What remember_for_me already saved this turn: the extractor must not store it a second time.
        from app.agent.memory import _block_note_lines

        saved_notes = _block_note_lines(await read_core(db, user, "learned", agent) or "")
    except Exception:  # noqa: BLE001
        saved_notes = []
    payload = await _extract_json(user_text, assistant_text, known, saved_notes)
    if payload and payload.get("discard") is not True:
        await apply_extract(db, user, agent, payload, job.session_id, known=known)
    try:
        from app.memory_engine.episodes import record_turn

        # A reply that left an action waiting for approval is half a story ("prepared it, not posted"). Saved as an
        # episode it outlives the approval and later contradicts what happened, so only finished turns are episodes.
        if not pending:
            await record_turn(db, user, agent, user_text, assistant_text, session_id=job.session_id)
    except Exception:  # noqa: BLE001 - a failed episode write must not fail the extract job
        log.info("episode memory skipped", exc_info=True)
    job.status = "done"
    job.last_error = ""
    return job.status
