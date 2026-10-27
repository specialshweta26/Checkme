"""Save a workflow, or teach one by watching the computer screen (no microphone)."""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.collab import TEACH_MAX_SECONDS
from app.models import AgentSkill, User, WorkspaceAgent

_TEACH: dict[str, dict[str, Any]] = {}


class SkillError(ValueError):
    pass


def _key(user_id) -> str:
    return str(user_id)


def teaching_session(user_id) -> dict[str, Any] | None:
    row = _TEACH.get(_key(user_id))
    if not row:
        return None
    if time.monotonic() - float(row.get("started_at") or 0) >= TEACH_MAX_SECONDS:
        return None
    return row


def record_computer_step(user_id, tool_name: str, args: dict | None = None) -> None:
    row = teaching_session(user_id)
    if not row:
        return
    steps = row.setdefault("steps", [])
    if len(steps) >= 400:
        return
    steps.append({"tool": tool_name, "args": args or {}, "at": time.time()})


def serialize_skill(row: AgentSkill) -> dict:
    return {
        "id": str(row.id),
        "agent_id": str(row.agent_id) if row.agent_id else None,
        "name": row.name,
        "kind": row.kind,
        "steps": row.steps or [],
        "instruction": row.instruction or "",
        "duration_ms": row.duration_ms or 0,
        "created_at": row.created_at.isoformat() if row.created_at else "",
    }


async def list_skills(db: AsyncSession, user: User, agent_id: uuid.UUID | None = None) -> list[dict]:
    stmt = select(AgentSkill).where(AgentSkill.user_id == user.id).order_by(AgentSkill.created_at.desc())
    if agent_id:
        stmt = stmt.where(AgentSkill.agent_id == agent_id)
    rows = (await db.execute(stmt)).scalars().all()
    return [serialize_skill(r) for r in rows]


async def save_skill(
    db: AsyncSession,
    user: User,
    *,
    name: str,
    agent_id: uuid.UUID | None = None,
    kind: str = "saved",
    steps: list | None = None,
    instruction: str = "",
    duration_ms: int = 0,
) -> AgentSkill:
    title = " ".join((name or "").split())
    if not title:
        raise SkillError("Skill name is required.")
    if agent_id:
        agent = await db.get(WorkspaceAgent, agent_id)
        if agent is None or agent.user_id != user.id:
            raise SkillError("Unknown agent")
    row = AgentSkill(
        user_id=user.id,
        agent_id=agent_id,
        name=title[:160],
        kind="taught" if kind == "taught" else "saved",
        steps=list(steps or []),
        instruction=(instruction or "")[:8000],
        duration_ms=max(0, int(duration_ms or 0)),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


def start_teaching(user: User, agent_id: uuid.UUID | None = None) -> dict:
    """Watch the visible computer screen for up to ten minutes. No microphone audio."""
    _TEACH[_key(user.id)] = {
        "agent_id": str(agent_id) if agent_id else None,
        "started_at": time.monotonic(),
        "started_wall": time.time(),
        "steps": [],
        "audio": False,
    }
    return {
        "ok": True,
        "teaching": True,
        "max_seconds": TEACH_MAX_SECONDS,
        "records_audio": False,
        "agent_id": str(agent_id) if agent_id else None,
    }


async def stop_teaching(db: AsyncSession, user: User, name: str = "") -> dict:
    row = _TEACH.pop(_key(user.id), None)
    if not row:
        raise SkillError("No teaching session is running.")
    elapsed_ms = int((time.monotonic() - float(row.get("started_at") or 0)) * 1000)
    title = " ".join((name or "").split()) or "Taught workflow"
    agent_id = None
    if row.get("agent_id"):
        try:
            agent_id = uuid.UUID(str(row["agent_id"]))
        except (TypeError, ValueError):
            agent_id = None
    skill = await save_skill(
        db,
        user,
        name=title,
        agent_id=agent_id,
        kind="taught",
        steps=row.get("steps") or [],
        instruction="Taught by watching the computer screen. No microphone audio was recorded.",
        duration_ms=min(elapsed_ms, TEACH_MAX_SECONDS * 1000),
    )
    return serialize_skill(skill)
