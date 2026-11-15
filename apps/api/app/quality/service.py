"""The quality loop (docs/QUALITY.md): how each agent is doing, measured from what the user does.

Three signals, all already in the user's hands, none needing a model call:

- **Ratings**: a thumbs up or down on a reply (``AgentFeedback``), with a reason and a note when it went wrong.
- **Routines**: scheduled runs that succeeded or failed (``ScheduledRun``, app/tasks).
- **Drafts**: how much of an inbox draft the user changed before sending (``InboxItem.sent_similarity``).

A thumbs-down keeps the request, the reply, the tools used and the turns before it, so it can become an agent test
case (``export_cases`` → ``evals/from_feedback.py``). Every query is scoped to the signed-in user.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentFeedback, ChatMessage, ChatSession, InboxItem, User, WorkspaceAgent

REASONS = {
    "wrong": "Wrong or made up",
    "incomplete": "Incomplete",
    "not_done": "Didn't do what I asked",
    "unwanted": "Did something I didn't want",
    "slow": "Too slow",
    "tone": "Wrong tone or style",
}
CLIP = 6000
HISTORY_TURNS = 6


class QualityError(ValueError):
    """Shown to the user."""


def _uuid(value, what: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError):
        raise QualityError(f"Unknown {what}.") from None


def _agent_of(msg: ChatMessage, sess: ChatSession) -> str:
    meta = msg.meta or {}
    return str(meta.get("agent_id") or sess.agent_id or "")


async def rate(
    db: AsyncSession, user: User, *, session_id, request_id: str, rating: int, reasons: list[str] | None = None,
    note: str = "", agent_id: str = "",
) -> dict:  # fmt: skip
    """Record (or change, or with rating 0 remove) the user's rating of one reply."""
    sid = _uuid(session_id, "conversation")
    sess = await db.get(ChatSession, sid)
    if sess is None or sess.user_id != user.id:
        raise QualityError("Unknown conversation.")
    if rating not in (-1, 0, 1):
        raise QualityError("A rating is 1 (good), -1 (bad) or 0 (clear).")
    request_id = str(request_id or "")[:64]
    if not request_id:
        raise QualityError("That reply can't be rated yet; reload the conversation and try again.")
    rows = (await db.execute(select(ChatMessage).where(ChatMessage.session_id == sid).order_by(ChatMessage.id))).scalars().all()
    replies = [m for m in rows if m.role == "assistant" and (m.request_id == request_id or (m.meta or {}).get("request_id") == request_id)]
    if agent_id:
        replies = [m for m in replies if _agent_of(m, sess) == str(agent_id)] or replies
    if not replies:
        raise QualityError("That reply wasn't found in this conversation.")
    reply = replies[-1]
    agent_key = _agent_of(reply, sess)
    existing = (
        await db.execute(
            select(AgentFeedback).where(
                AgentFeedback.user_id == user.id, AgentFeedback.session_id == sid,
                AgentFeedback.request_id == request_id, AgentFeedback.agent_key == agent_key,
            )
        )
    ).scalar_one_or_none()  # fmt: skip
    if rating == 0:
        if existing is not None:
            await _withdraw_lessons(db, existing.id)
            await db.delete(existing)
            await db.commit()
        return {"rating": 0, "request_id": request_id, "agent_id": agent_key or None}
    at = rows.index(reply)
    asked = next((m for m in reversed(rows[:at]) if m.role == "user"), None)
    start = rows.index(asked) if asked is not None else at
    history = [{"role": m.role, "content": (m.content or "")[:1500]} for m in rows[:start] if m.role in ("user", "assistant")][-HISTORY_TURNS:]
    row = existing or AgentFeedback(user_id=user.id, session_id=sid, request_id=request_id, agent_key=agent_key)
    row.agent_id = _uuid(agent_key, "agent") if agent_key else None
    row.rating = rating
    row.reasons = [r for r in (reasons or []) if r in REASONS][:6] if rating < 0 else []
    row.note = (note or "").strip()[:2000] if rating < 0 else ""
    row.prompt = (asked.content if asked is not None else "")[:CLIP]
    row.reply = (reply.content or "")[:CLIP]
    row.tools = [str(t) for t in ((reply.meta or {}).get("used_tools") or [])][:40]
    row.history = history
    row.updated_at = datetime.now(timezone.utc)
    if existing is None:
        db.add(row)
    if rating > 0 and existing is not None:
        await _withdraw_lessons(db, existing.id)
    await db.commit()
    learn = rating < 0 and bool(row.agent_id) and bool(row.reasons or row.note)
    if learn:
        from app.quality import lessons

        lessons.propose_later(row.id)  # the agent drafts a lesson for the user to accept (app/quality/lessons.py)
    return {"rating": rating, "request_id": request_id, "agent_id": agent_key or None, "lesson": "drafting" if learn else None}


async def _withdraw_lessons(db: AsyncSession, feedback_id) -> None:
    """A rating cleared or turned good: its lesson, if still waiting for review, goes."""
    from app.models import AgentLesson

    rows = (await db.execute(select(AgentLesson).where(AgentLesson.feedback_id == feedback_id, AgentLesson.status == "proposed"))).scalars().all()
    for r in rows:
        await db.delete(r)


async def ratings_for(db: AsyncSession, user: User, session_id) -> dict[str, int]:
    """{"<request_id>:<agent_id>": rating} for one conversation, so the chat shows what was already rated."""
    sid = _uuid(session_id, "conversation")
    rows = (await db.execute(select(AgentFeedback).where(AgentFeedback.user_id == user.id, AgentFeedback.session_id == sid))).scalars().all()
    return {f"{r.request_id}:{r.agent_key}": r.rating for r in rows}


def _since(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=max(1, min(int(days or 30), 365)))


def _row(r: AgentFeedback, names: dict) -> dict:
    agent = names.get(r.agent_key) or {}
    return {
        "id": str(r.id),
        "rating": r.rating,
        "reasons": r.reasons or [],
        "reason_labels": [REASONS.get(x, x) for x in r.reasons or []],
        "note": r.note,
        "prompt": r.prompt,
        "reply": r.reply,
        "tools": r.tools or [],
        "session_id": str(r.session_id),
        "request_id": r.request_id,
        "agent_id": r.agent_key or None,
        "agent_name": agent.get("name", ""),
        "agent_color": agent.get("color", ""),
        "at": (r.updated_at or r.created_at).isoformat(),
    }


async def _names(db: AsyncSession, user: User) -> dict[str, dict]:
    rows = (await db.execute(select(WorkspaceAgent).where(WorkspaceAgent.user_id == user.id))).scalars().all()
    return {str(a.id): {"name": a.name, "color": a.color or ""} for a in rows}


async def list_feedback(db: AsyncSession, user: User, *, rating: int | None = None, agent_id: str = "", days: int = 30, limit: int = 100) -> list[dict]:
    stmt = select(AgentFeedback).where(AgentFeedback.user_id == user.id, AgentFeedback.updated_at >= _since(days))
    if rating in (-1, 1):
        stmt = stmt.where(AgentFeedback.rating == rating)
    if agent_id:
        stmt = stmt.where(AgentFeedback.agent_key == str(agent_id))
    rows = (await db.execute(stmt.order_by(AgentFeedback.updated_at.desc()).limit(max(1, min(limit, 500))))).scalars().all()
    names = await _names(db, user)
    return [_row(r, names) for r in rows]


def draft_bucket(similarity: float) -> str:
    """Same buckets as the Usage page: sent unchanged, lightly edited, or rewritten."""
    return "unchanged" if similarity >= 0.97 else "light" if similarity >= 0.7 else "rewritten"


async def summary(db: AsyncSession, user: User, *, agent_id: str = "", days: int = 30) -> dict:
    """Ratings, routine outcomes and draft edits over ``days``, overall and per agent."""
    from app.tasks.service import summary as routine_summary

    since = _since(days)
    names = await _names(db, user)
    stmt = select(AgentFeedback).where(AgentFeedback.user_id == user.id, AgentFeedback.updated_at >= since)
    if agent_id:
        stmt = stmt.where(AgentFeedback.agent_key == str(agent_id))
    rows = (await db.execute(stmt)).scalars().all()
    per: dict[str, dict] = {}
    reasons: dict[str, int] = {}
    for r in rows:
        a = per.setdefault(r.agent_key, {"agent_id": r.agent_key or None, "agent_name": (names.get(r.agent_key) or {}).get("name", ""),
                                         "agent_color": (names.get(r.agent_key) or {}).get("color", ""), "up": 0, "down": 0})  # fmt: skip
        a["up" if r.rating > 0 else "down"] += 1
        if r.rating < 0:
            for x in r.reasons or []:
                reasons[x] = reasons.get(x, 0) + 1
    routines = await routine_summary(db, user, agent_id=agent_id or None, days=days)
    for ra in routines.get("agents") or []:
        a = per.setdefault(ra["agent_id"] or "", {"agent_id": ra["agent_id"], "agent_name": ra["agent_name"], "agent_color": ra["agent_color"], "up": 0, "down": 0})
        a["routine_ok"], a["routine_failed"] = ra["succeeded"], ra["failed"]
    for a in per.values():
        rated = a["up"] + a["down"]
        a["satisfaction"] = round(100 * a["up"] / rated) if rated else None
        a.setdefault("routine_ok", 0)
        a.setdefault("routine_failed", 0)
    drafts = {"unchanged": 0, "light": 0, "rewritten": 0}
    if not agent_id:  # drafts belong to the inbox, not to one agent
        sims = (
            await db.execute(
                select(InboxItem.sent_similarity).where(
                    InboxItem.user_id == user.id, InboxItem.sent_similarity.is_not(None), InboxItem.sent_at >= since
                )
            )
        ).scalars().all()
        for s in sims:
            drafts[draft_bucket(float(s))] += 1
    up = sum(a["up"] for a in per.values())
    down = sum(a["down"] for a in per.values())
    sent = sum(drafts.values())
    finished = routines.get("succeeded", 0) + routines.get("failed", 0)
    return {
        "days": days,
        "up": up,
        "down": down,
        "satisfaction": round(100 * up / (up + down)) if up + down else None,
        "routines": {"succeeded": routines.get("succeeded", 0), "failed": routines.get("failed", 0),
                     "success_rate": round(100 * routines.get("succeeded", 0) / finished) if finished else None},  # fmt: skip
        "drafts": {**drafts, "sent": sent, "kept_rate": round(100 * (drafts["unchanged"] + drafts["light"]) / sent) if sent else None},
        "reasons": sorted(({"reason": k, "label": REASONS.get(k, k), "count": v} for k, v in reasons.items()), key=lambda x: -x["count"]),
        "agents": sorted((a for a in per.values() if a["agent_name"]), key=lambda a: a["agent_name"].lower()),
    }


async def export_cases(db: AsyncSession, user: User, *, ids: list[str] | None = None, days: int = 90) -> list[dict]:
    """Thumbs-downs as agent test-case stubs (evals/from_feedback.py turns them into scenarios)."""
    stmt = select(AgentFeedback).where(AgentFeedback.user_id == user.id, AgentFeedback.rating < 0, AgentFeedback.updated_at >= _since(days))
    if ids:
        stmt = stmt.where(AgentFeedback.id.in_([_uuid(i, "rating") for i in ids]))
    rows = (await db.execute(stmt.order_by(AgentFeedback.updated_at.desc()).limit(200))).scalars().all()
    names = await _names(db, user)
    out = []
    for r in rows:
        agent = (names.get(r.agent_key) or {}).get("name", "")
        why = "; ".join([REASONS.get(x, x) for x in r.reasons or []] + ([r.note] if r.note else []))
        out.append(
            {
                "name": f"feedback {str(r.id)[:8]}: {' '.join((r.prompt or '').split())[:50]}",
                "message": r.prompt,
                "history": r.history or [],
                "agent": agent,
                "bad_reply": r.reply,
                "tools_used": r.tools or [],
                "why": why,
                # To fill in when the case is added: what the agent must or mustn't do (see evals/scenarios.py).
                "must_call": [],
                "must_not_call": [],
                "reply_has": [],
                "reply_lacks": [],
            }
        )
    return out
