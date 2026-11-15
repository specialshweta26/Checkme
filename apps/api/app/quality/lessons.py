"""The learning loop (docs/QUALITY.md "Lessons"): agents learn from what the user says went wrong.

A thumbs-down with a reason or a note makes the agent propose one short lesson: a general rule for next time
("When asked for an inbox summary, list which emails need a reply, not just counts."). Nothing changes until the user
accepts it (as written or edited); accepted lessons sit on the agent (``WorkspaceAgent.lessons``) and go into its
prompt after its own instructions, which are never rewritten. The user can remove a lesson at any time.

Writing the lesson takes one small-model call. Without a model (or if it fails), the user's own note becomes the
lesson, so the loop still works; a thumbs-down with no note and no model proposes nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentFeedback, AgentLesson, User, WorkspaceAgent

log = logging.getLogger(__name__)

MAX_LESSONS = 12  # in the prompt, newest first
MAX_CHARS = 240
_KEEP: set[asyncio.Task] = set()


class LessonError(ValueError):
    """Shown to the user."""


def lessons_prompt(agent) -> str:
    """The accepted lessons as a prompt section (empty when there are none)."""
    rows = [x for x in (getattr(agent, "lessons", None) or []) if isinstance(x, dict) and str(x.get("text") or "").strip()]
    if not rows:
        return ""
    lines = "\n".join(f"- {str(x['text']).strip()[:MAX_CHARS]}" for x in rows[-MAX_LESSONS:])
    return "Lessons from this user's feedback on your earlier replies (follow them):\n" + lines + "\n"


def _clean(text: str) -> str:
    one = " ".join(str(text or "").split()).strip(" -•")
    return one[:MAX_CHARS]


async def draft(prompt: str, reply: str, why: str, note: str, existing: list[str], llm=None) -> str:
    """One general rule for next time, or "" when there's nothing to learn."""
    fallback = _clean(note) if len(_clean(note)) >= 12 else ""
    if llm is None:
        from app.config import get_settings

        if not get_settings().openai_api_key:
            return fallback
        from functools import partial

        from app.llm import chat_complete, small_model

        llm = partial(chat_complete, model=small_model())
    messages = [
        {"role": "system", "content": "You turn a user's complaint about an AI assistant's reply into one short lesson the assistant follows from now on."},
        {
            "role": "user",
            "content": json.dumps(
                {"user_asked": prompt[:1500], "assistant_replied": reply[:2000], "what_went_wrong": why, "user_note": note[:800], "lessons_it_already_has": existing[:12]},
                ensure_ascii=False,
            )
            + '\n\nReply in JSON: {"lesson": "..."}. The lesson is one imperative sentence of at most 200 characters, general '
            "enough to apply to similar requests (not only this one), about what to do differently. Use the user's note "
            'when there is one. Return {"lesson": ""} if the complaint is not about the reply (for example only that it was '
            "slow) or an existing lesson already covers it.",
        },
    ]
    try:
        resp = await asyncio.wait_for(llm(messages, response_format={"type": "json_object"}, max_tokens=160), 20)
        raw = resp.choices[0].message.content if hasattr(resp, "choices") else str(resp)
        text = _clean(json.loads(raw or "{}").get("lesson") or "")
        return text if text and text.lower() not in {e.lower() for e in existing} else ""
    except Exception as exc:  # noqa: BLE001 - the user's note still works
        log.info("lesson draft unavailable (%s)", type(exc).__name__)
        return fallback


async def propose(db: AsyncSession, feedback_id, llm=None) -> AgentLesson | None:
    """Draft a lesson from one thumbs-down (replacing an earlier proposal from the same rating)."""
    fb = await db.get(AgentFeedback, uuid.UUID(str(feedback_id)))
    if fb is None or fb.rating >= 0 or not fb.agent_id:
        return None
    agent = await db.get(WorkspaceAgent, fb.agent_id)
    if agent is None or agent.user_id != fb.user_id:
        return None
    from app.quality.service import REASONS

    why = "; ".join(REASONS.get(x, x) for x in fb.reasons or [])
    existing = [str(x.get("text") or "") for x in agent.lessons or [] if isinstance(x, dict)]
    text = await draft(fb.prompt or "", fb.reply or "", why, fb.note or "", existing, llm=llm)
    old = (await db.execute(select(AgentLesson).where(AgentLesson.feedback_id == fb.id, AgentLesson.status == "proposed"))).scalars().all()
    for row in old:
        await db.delete(row)
    if not text:
        await db.commit()
        return None
    row = AgentLesson(user_id=fb.user_id, agent_id=agent.id, feedback_id=fb.id, text=text, why="; ".join(x for x in [why, fb.note] if x)[:1000])
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


def propose_later(feedback_id) -> None:
    """Draft the lesson in the background, so rating a reply stays instant."""

    async def run():
        from app.db import SessionLocal

        try:
            async with SessionLocal() as db:
                await propose(db, feedback_id)
        except Exception:  # noqa: BLE001
            log.info("lesson proposal failed", exc_info=True)

    task = asyncio.create_task(run())
    _KEEP.add(task)
    task.add_done_callback(_KEEP.discard)


def _out(row: AgentLesson, names: dict) -> dict:
    agent = names.get(str(row.agent_id)) or {}
    return {
        "id": str(row.id),
        "agent_id": str(row.agent_id),
        "agent_name": agent.get("name", ""),
        "agent_color": agent.get("color", ""),
        "text": row.text,
        "why": row.why,
        "status": row.status,
        "source": row.source,
        "feedback_id": str(row.feedback_id) if row.feedback_id else None,
        "at": (row.decided_at or row.created_at).isoformat(),
    }


async def list_lessons(db: AsyncSession, user: User, *, status: str = "", agent_id: str = "") -> list[dict]:
    stmt = select(AgentLesson).where(AgentLesson.user_id == user.id)
    if status:
        stmt = stmt.where(AgentLesson.status == status)
    if agent_id:
        stmt = stmt.where(AgentLesson.agent_id == uuid.UUID(str(agent_id)))
    rows = (await db.execute(stmt.order_by(AgentLesson.created_at.desc()).limit(200))).scalars().all()
    from app.quality.service import _names

    names = await _names(db, user)
    return [_out(r, names) for r in rows]


async def _owned(db: AsyncSession, user: User, lesson_id) -> AgentLesson:
    try:
        row = await db.get(AgentLesson, uuid.UUID(str(lesson_id)))
    except (TypeError, ValueError):
        row = None
    if row is None or row.user_id != user.id:
        raise LessonError("Unknown lesson.")
    return row


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def accept(db: AsyncSession, user: User, lesson_id, text: str = "") -> dict:
    """Use the lesson from now on (optionally reworded by the user)."""
    row = await _owned(db, user, lesson_id)
    agent = await db.get(WorkspaceAgent, row.agent_id)
    if agent is None or agent.user_id != user.id:
        raise LessonError("That agent no longer exists.")
    if text.strip():
        row.text = _clean(text)
    if not row.text:
        raise LessonError("A lesson needs some text.")
    row.status, row.decided_at = "accepted", _now()
    kept = [x for x in agent.lessons or [] if isinstance(x, dict) and x.get("id") != str(row.id)]
    agent.lessons = kept + [{"id": str(row.id), "text": row.text, "source": row.source, "at": row.decided_at.isoformat()}]
    await db.commit()
    from app.quality.service import _names

    return _out(row, await _names(db, user))


async def dismiss(db: AsyncSession, user: User, lesson_id) -> dict:
    row = await _owned(db, user, lesson_id)
    row.status, row.decided_at = "dismissed", _now()
    await db.commit()
    return {"ok": True}


async def remove(db: AsyncSession, user: User, lesson_id) -> dict:
    """Stop using an accepted lesson."""
    row = await _owned(db, user, lesson_id)
    agent = await db.get(WorkspaceAgent, row.agent_id)
    if agent is not None:
        agent.lessons = [x for x in agent.lessons or [] if isinstance(x, dict) and x.get("id") != str(row.id)]
    row.status, row.decided_at = "dismissed", _now()
    await db.commit()
    return {"ok": True}


async def add(db: AsyncSession, user: User, agent_id, text: str) -> dict:
    """A lesson the user writes themselves: in use at once."""
    try:
        agent = await db.get(WorkspaceAgent, uuid.UUID(str(agent_id)))
    except (TypeError, ValueError):
        agent = None
    if agent is None or agent.user_id != user.id:
        raise LessonError("Unknown agent.")
    if not _clean(text):
        raise LessonError("A lesson needs some text.")
    row = AgentLesson(user_id=user.id, agent_id=agent.id, text=_clean(text), source="manual", status="proposed")
    db.add(row)
    await db.flush()
    return await accept(db, user, row.id)
