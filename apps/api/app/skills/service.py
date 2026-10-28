"""Save a workflow, or teach one by watching the computer screen (no microphone)."""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

from sqlalchemy import or_, select
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
        stmt = stmt.where(or_(AgentSkill.agent_id == agent_id, AgentSkill.agent_id.is_(None)))
    rows = (await db.execute(stmt)).scalars().all()
    return [serialize_skill(r) for r in rows]


async def get_skill(
    db: AsyncSession, user: User, skill_id: str = "", name: str = "", agent_id: uuid.UUID | None = None
) -> AgentSkill | None:
    if skill_id:
        try:
            uid = uuid.UUID(str(skill_id))
        except (TypeError, ValueError):
            uid = None
        if uid is not None:
            row = await db.get(AgentSkill, uid)
            if row is not None and row.user_id == user.id:
                return row
    title = " ".join((name or "").split())
    if not title:
        return None
    stmt = select(AgentSkill).where(AgentSkill.user_id == user.id, AgentSkill.name.ilike(title))
    if agent_id:
        stmt = stmt.where(or_(AgentSkill.agent_id == agent_id, AgentSkill.agent_id.is_(None)))
    stmt = stmt.order_by(AgentSkill.created_at.desc())
    return (await db.execute(stmt)).scalars().first()


async def update_skill(
    db: AsyncSession,
    user: User,
    *,
    skill_id: str = "",
    name: str = "",
    instruction: str | None = None,
    new_name: str = "",
    agent_id: uuid.UUID | None = None,
) -> AgentSkill:
    row = await get_skill(db, user, skill_id, name, agent_id)
    if row is None:
        raise SkillError("Unknown skill.")
    if new_name:
        title = " ".join(new_name.split())
        if not title:
            raise SkillError("Skill name is required.")
        row.name = title[:160]
    if instruction is not None:
        row.instruction = instruction[:8000]
    await db.commit()
    await db.refresh(row)
    return row


async def delete_skill(
    db: AsyncSession, user: User, skill_id: str = "", name: str = "", agent_id: uuid.UUID | None = None
) -> dict:
    row = await get_skill(db, user, skill_id, name, agent_id)
    if row is None:
        raise SkillError("Unknown skill.")
    payload = serialize_skill(row)
    await db.delete(row)
    await db.commit()
    return {"ok": True, "deleted": payload}


def match_skills(skills: list[dict], query: str, limit: int = 3) -> list[dict]:
    q = (query or "").lower()
    if not q or not skills:
        return []
    words = [w for w in re.findall(r"[a-z0-9]{3,}", q)]
    scored: list[tuple[int, dict]] = []
    for skill in skills:
        hay = f"{skill.get('name') or ''} {skill.get('instruction') or ''}".lower()
        score = 0
        if (skill.get("name") or "").lower() in q or q in hay:
            score += 5
        score += sum(2 if w in (skill.get("name") or "").lower() else 1 for w in words if w in hay)
        if score:
            scored.append((score, skill))
    scored.sort(key=lambda item: (-item[0], item[1].get("name") or ""))
    return [row for _, row in scored[: max(1, min(int(limit or 3), 6))]]


def skills_prompt(skills: list[dict] | None) -> str:
    rows = [s for s in (skills or []) if s.get("name")]
    if not rows:
        return ""
    lines = ["Saved skills that match this request (follow them unless the user says otherwise):"]
    for skill in rows:
        bit = (skill.get("instruction") or "").strip() or "(taught steps on the computer screen)"
        lines.append(f"- {skill['name']}: {bit[:400]}")
    return "\n".join(lines) + "\n"


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
