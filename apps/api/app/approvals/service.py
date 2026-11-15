"""One approvals queue (docs/APPROVALS.md): everything any agent is waiting on you for, in one place.

Kinds:
- ``plan``: a background task waiting for its plan to be started (docs/PLANS.md). Batchable.
- ``action``: a background job paused on an action that needs your yes (send, push, merge, pay…). Batchable.
- ``question``: a background job asked you something. Answer with one of its options, or in the chat.
- ``handoff``: a job handed you the computer (sign in, solve a check). Finish it in the chat.
- ``chat``: a chat reply prepared an action and is waiting for a yes. These approvals are short-lived (minutes), so
  they are listed while still valid and approved through the chat itself (the same path as typing "yes").

Batch decisions on plans and actions run the existing job transitions (``plans.approve``, ``jobs.approve`` /
``jobs.decline``, ``jobs.cancel_tree``); nothing is approved that the job itself would not accept.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.confirm import read_confirm
from app.jobs import plans
from app.jobs import service as jobs
from app.models import AgentJob, ChatMessage, ChatSession, User, WorkspaceAgent

WAITING = ("planned", "waiting_approval", "waiting_input")
BATCHABLE = ("plan", "action")
MAX_BATCH = 50
_PREVIEW_KEYS = ("summary", "to", "recipient", "subject", "repo", "branch", "pr", "amount", "url", "command", "title")


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def preview_lines(preview: Any, limit: int = 4) -> list[str]:
    """A few readable lines from an approval preview (its shape differs per tool)."""
    if isinstance(preview, str):
        return [preview[:300]] if preview.strip() else []
    if not isinstance(preview, dict):
        return []
    out: list[str] = []
    for key in _PREVIEW_KEYS:
        value = preview.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            out.append(f"{key.replace('_', ' ').capitalize()}: {str(value)[:200]}")
        elif isinstance(value, list) and value and all(isinstance(v, str) for v in value):
            out.append(f"{key.capitalize()}: {', '.join(value)[:200]}")
        if len(out) >= limit:
            break
    return out


def _is_handoff(card: dict) -> bool:
    return card.get("kind") == "handoff" or (card.get("preview") or {}).get("kind") == "handoff"


def job_items(job: AgentJob, agent: dict) -> list[dict]:
    meta = job.meta or {}
    base = {"job_id": str(job.id), "session_id": str(job.session_id) if job.session_id else None, "job_title": job.title,
            "since": _iso(job.updated_at or job.created_at), **agent}  # fmt: skip
    if job.status == "planned":
        plan = plans.serialize(job) or {}
        return [{**base, "id": f"plan:{job.id}", "kind": "plan", "title": job.title, "lines": list(plan.get("steps") or []),
                 "plan": plan}]  # fmt: skip
    if job.status == "waiting_input":
        q = meta.get("question") or {}
        return [{**base, "id": f"question:{job.id}", "kind": "question", "title": str(q.get("question") or "The agent has a question."),
                 "options": list(q.get("options") or []), "lines": []}]  # fmt: skip
    out = []
    for card in meta.get("pending") or []:
        token = str(card.get("token") or "")
        kind = "handoff" if _is_handoff(card) else "action"
        data = read_confirm(token) if token else None
        out.append({**base, "id": f"{kind}:{job.id}:{token[:16]}", "kind": kind, "token": token, "title": str(card.get("title") or "An action"),
                    "lines": preview_lines(card.get("preview")), "expired": kind == "action" and not data,
                    "expires_at": int(data["exp"]) if data else None})  # fmt: skip
    return out


async def _agents(db: AsyncSession, user: User) -> dict[str, dict]:
    rows = (await db.execute(select(WorkspaceAgent).where(WorkspaceAgent.user_id == user.id))).scalars().all()
    return {str(a.id): {"agent_id": str(a.id), "agent_name": a.name, "agent_color": getattr(a, "color", "") or "",
                        "agent_avatar": getattr(a, "avatar", "") or ""} for a in rows}  # fmt: skip


async def chat_items(db: AsyncSession, user: User, agents: dict[str, dict]) -> list[dict]:
    """Still-valid approvals on the latest reply of each recent one-to-one chat."""
    sessions = (
        await db.execute(
            select(ChatSession).where(ChatSession.user_id == user.id, ChatSession.agent_id.isnot(None))
            .order_by(ChatSession.updated_at.desc()).limit(40)
        )
    ).scalars().all()  # fmt: skip
    if not sessions:
        return []
    latest = (
        select(ChatMessage.session_id, func.max(ChatMessage.id).label("mid"))
        .where(ChatMessage.session_id.in_([s.id for s in sessions]), ChatMessage.role == "assistant")
        .group_by(ChatMessage.session_id).subquery()
    )  # fmt: skip
    messages = (await db.execute(select(ChatMessage).join(latest, ChatMessage.id == latest.c.mid))).scalars().all()
    by_id = {s.id: s for s in sessions}
    out = []
    now = time.time()
    for msg in messages:
        sess = by_id.get(msg.session_id)
        for card in (msg.meta or {}).get("pending_confirms") or []:
            token = str(card.get("token") or "")
            data = read_confirm(token) if token else None
            if not data or data.get("exp", 0) < now:
                continue
            agent = agents.get(str(sess.agent_id), {"agent_id": str(sess.agent_id), "agent_name": "", "agent_color": "", "agent_avatar": ""})
            kind = "handoff" if _is_handoff(card) else "chat"
            out.append({**agent, "id": f"{kind}:{msg.id}:{token[:16]}", "kind": kind, "token": token, "session_id": str(msg.session_id),
                        "message_id": msg.id, "title": str(card.get("title") or "An action"), "lines": preview_lines(card.get("preview")),
                        "since": _iso(msg.created_at), "expires_at": int(data["exp"]), "job_id": None})  # fmt: skip
    return out


async def queue(db: AsyncSession, user: User) -> dict:
    agents = await _agents(db, user)
    rows = (
        await db.execute(
            select(AgentJob).where(AgentJob.user_id == user.id, AgentJob.status.in_(WAITING))
            .order_by(AgentJob.updated_at.desc()).limit(100)
        )
    ).scalars().all()  # fmt: skip
    items: list[dict] = []
    for job in rows:
        agent = agents.get(str(job.agent_id), {"agent_id": str(job.agent_id) if job.agent_id else None,
                                               "agent_name": (job.meta or {}).get("agent_name") or "", "agent_color": "", "agent_avatar": ""})  # fmt: skip
        items.extend(job_items(job, agent))
    items.extend(await chat_items(db, user, agents))
    items.sort(key=lambda i: i.get("since") or "", reverse=True)
    counts: dict[str, int] = {}
    for i in items:
        counts[i["kind"]] = counts.get(i["kind"], 0) + 1
    return {"items": items, "count": len(items), "counts": counts}


async def count(db: AsyncSession, user: User) -> int:
    return (await queue(db, user))["count"]


async def decide(db: AsyncSession, user: User, decisions: list[dict]) -> list[dict]:
    """Apply approve/decline to plan and action items. Returns one result per decision; one failing doesn't stop the rest."""
    from app.jobs.worker import wake

    if len(decisions) > MAX_BATCH:
        raise ValueError(f"At most {MAX_BATCH} at a time.")
    owner = SimpleNamespace(id=user.id)  # a rollback after a failed item expires ORM objects; keep the id
    results, woke = [], False
    for d in decisions:
        kind, job_id, token, approve = d.get("kind"), str(d.get("job_id") or ""), str(d.get("token") or ""), bool(d.get("approve"))
        res = {"id": d.get("id"), "ok": False}
        try:
            if kind not in BATCHABLE:
                raise jobs.JobError("Only plans and actions can be decided here; open the chat for this one.")
            try:
                job = await jobs.get_job(db, owner, job_id)
            except (LookupError, ValueError):
                raise jobs.JobError("That task no longer exists.") from None
            if kind == "plan":
                if approve:
                    events = plans.approve(job)
                else:
                    await jobs.cancel_tree(db, job)
                    events = []
            else:
                events = jobs.approve(job, token) if approve else jobs.decline(job, token)
            for ev in events:
                db.add(ev)
            await db.commit()
            woke = woke or job.status == "queued"
            res.update(ok=True, status=job.status)
        except (jobs.JobError, plans.PlanError) as exc:
            await db.rollback()
            res["error"] = str(exc)
        results.append(res)
    if woke:
        wake()
    return results


async def answer(db: AsyncSession, user: User, job_id: str, text: str) -> dict:
    """Answer a job's question with one of its options (same as the button in the chat)."""
    from app.jobs.worker import wake

    job = await jobs.get_job(db, user, job_id)
    events = jobs.answer(job, text)
    for ev in events:
        db.add(ev)
    if job.session_id is not None:
        db.add(ChatMessage(session_id=job.session_id, user_id=user.id, role="user", content=text.strip()[:2000],
                           meta={"job_answer": str(job.id)}, request_id=f"answer-{str(job.id)[:8]}"))  # fmt: skip
    await db.commit()
    wake()
    return jobs.serialize_job(job)


async def dismiss_chat(db: AsyncSession, user: User, message_id: int, token: str) -> bool:
    """Decline a chat approval from the queue: drop the card from that reply so it can't be run."""
    msg = await db.get(ChatMessage, int(message_id))
    if msg is None or msg.user_id != user.id:
        return False
    meta = dict(msg.meta or {})
    cards = list(meta.get("pending_confirms") or [])
    left = [c for c in cards if str(c.get("token") or "") != token]
    if len(left) == len(cards):
        return False
    meta["pending_confirms"] = left
    meta["declined"] = list(meta.get("declined") or []) + [str(next(c for c in cards if c.get("token") == token).get("title") or "an action")]
    msg.meta = meta
    await db.commit()
    return True
