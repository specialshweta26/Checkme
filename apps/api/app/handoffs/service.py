"""Agent-to-agent handoffs (docs/HANDOFFS.md).

Every time one agent hands work to another (``delegate`` inside a background job, ``ask_teammate`` in a group chat) a
receipt is written to ``agent_messages``:

- ``sent``: handed over, not picked up yet (a delegated job waiting in the queue).
- ``received``: the receiving agent started on it.
- ``done`` / ``failed`` / ``cancelled``: it finished; ``reply`` holds the first part of its answer.
- ``refused``: the receiver's rule doesn't take work from that sender. Nothing ran.

Each agent has a rule, ``workspace_agents.accepts_from``: ``anyone`` (the default), ``selected`` (only the listed agents)
or ``nobody`` (only you). You can always message any agent yourself, and an agent can always hand work to itself.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentMessage, WorkspaceAgent

MODES = ("anyone", "selected", "nobody")
TERMINAL = {"done": "done", "failed": "failed", "cancelled": "cancelled"}
TASK_MAX = 2000
REPLY_MAX = 1500


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clip(text: Any, n: int) -> str:
    s = str(text or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def rule(agent: Any) -> dict:
    """The agent's rule, normalized: {"mode": ..., "agents": [ids]}."""
    raw = getattr(agent, "accepts_from", None) or {}
    mode = raw.get("mode") if isinstance(raw, dict) else None
    mode = mode if mode in MODES else "anyone"
    agents = [str(a) for a in (raw.get("agents") or [])] if isinstance(raw, dict) and mode == "selected" else []
    return {"mode": mode, "agents": agents}


def clean_rule(mode: str, agents: list | None, own_ids: set[str], self_id: str) -> dict:
    """Validate a rule the user set. Only the user's own agents can be listed; the agent itself is never needed."""
    mode = (mode or "").strip().lower()
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    picked = [str(a) for a in (agents or []) if str(a) in own_ids and str(a) != self_id]
    picked = list(dict.fromkeys(picked))
    if mode == "selected" and not picked:
        raise ValueError("Pick at least one agent, or choose “Only you”.")
    return {"mode": mode, "agents": picked if mode == "selected" else []}


def refusal(target: Any, sender: Any, names: dict[str, str] | None = None) -> str | None:
    """Why ``sender`` may not hand work to ``target``, or None when it may. A missing sender is the user."""
    if sender is None or target is None or str(getattr(sender, "id", "")) == str(getattr(target, "id", "")):
        return None
    r = rule(target)
    sid = str(getattr(sender, "id", ""))
    if r["mode"] == "anyone" or (r["mode"] == "selected" and sid in r["agents"]):
        return None
    tname = getattr(target, "name", "") or "That agent"
    if r["mode"] == "nobody":
        return f"{tname} only takes work from the user, not from other agents. Do this part yourself or ask the user to message {tname}."
    allowed = ", ".join((names or {}).get(a, "") for a in r["agents"] if (names or {}).get(a)) or "certain agents"
    return f"{tname} only takes work from {allowed} and the user. Do this part yourself or ask the user to message {tname}."


def record(db: AsyncSession, user_id, sender: Any, target: Any, channel: str, task: str, *, status: str = "sent", reason: str = "",
           job_id=None, parent_job_id=None, session_id=None) -> AgentMessage:  # fmt: skip
    row = AgentMessage(
        id=uuid.uuid4(),
        user_id=user_id,
        from_agent_id=getattr(sender, "id", None),
        to_agent_id=getattr(target, "id", None),
        from_name=str(getattr(sender, "name", "") or "")[:80],
        to_name=str(getattr(target, "name", "") or "")[:80],
        channel=channel,
        status=status,
        task=_clip(task, TASK_MAX),
        reason=_clip(reason, 400),
        job_id=job_id,
        parent_job_id=parent_job_id,
        session_id=session_id,
        created_at=_now(),
        finished_at=_now() if status == "refused" else None,
    )
    db.add(row)
    return row


def mark_received(row: AgentMessage) -> None:
    if row.status == "sent":
        row.status = "received"
        row.received_at = _now()


def mark_finished(row: AgentMessage, status: str, reply: str = "") -> None:
    if row.status in TERMINAL or row.status == "refused":
        return
    row.received_at = row.received_at or _now()
    row.status = TERMINAL.get(status, "failed")
    row.reply = _clip(reply, REPLY_MAX)
    row.finished_at = _now()


async def job_started(db: AsyncSession, job_id) -> None:
    """The worker picked up a delegated job: its receipt is now received."""
    await db.execute(
        update(AgentMessage)
        .where(AgentMessage.job_id == job_id, AgentMessage.status == "sent")
        .values(status="received", received_at=_now())
    )  # fmt: skip


async def job_finished(db: AsyncSession, job: Any) -> list[AgentMessage]:
    """A delegated job ended: close its receipts with the outcome."""
    rows = (await db.execute(select(AgentMessage).where(AgentMessage.job_id == job.id))).scalars().all()
    body = job.result if job.status == "done" else (job.error or job.status)
    for row in rows:
        mark_finished(row, job.status, body or "")
    return list(rows)


def completion_text(job: Any, agent_name: str) -> str:
    who = agent_name or "The delegated job"
    title = getattr(job, "title", "") or "its task"
    if job.status == "done":
        return f"{who} finished: {title}"
    if job.status == "cancelled":
        return f"{who}'s task was cancelled: {title}"
    return f"{who} couldn't finish: {title}"


def serialize(row: AgentMessage) -> dict:
    iso = lambda d: d.isoformat() if d else None
    return {
        "id": str(row.id),
        "from_agent_id": str(row.from_agent_id) if row.from_agent_id else None,
        "to_agent_id": str(row.to_agent_id) if row.to_agent_id else None,
        "from_name": row.from_name,
        "to_name": row.to_name,
        "channel": row.channel,
        "status": row.status,
        "task": row.task,
        "reply": row.reply,
        "reason": row.reason,
        "job_id": str(row.job_id) if row.job_id else None,
        "parent_job_id": str(row.parent_job_id) if row.parent_job_id else None,
        "session_id": str(row.session_id) if row.session_id else None,
        "created_at": iso(row.created_at),
        "received_at": iso(row.received_at),
        "finished_at": iso(row.finished_at),
    }


async def history(db: AsyncSession, user_id, agent_id=None, limit: int = 50) -> list[dict]:
    q = select(AgentMessage).where(AgentMessage.user_id == user_id)
    if agent_id is not None:
        q = q.where(or_(AgentMessage.from_agent_id == agent_id, AgentMessage.to_agent_id == agent_id))
    rows = (await db.execute(q.order_by(AgentMessage.created_at.desc()).limit(max(1, min(int(limit), 200))))).scalars().all()
    return [serialize(r) for r in rows]


async def agent_names(db: AsyncSession, user_id) -> dict[str, str]:
    rows = (await db.execute(select(WorkspaceAgent.id, WorkspaceAgent.name).where(WorkspaceAgent.user_id == user_id))).all()
    return {str(i): n for i, n in rows}
