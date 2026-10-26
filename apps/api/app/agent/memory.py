from __future__ import annotations

import logging
import time
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.llm import embed_one
from app.models import MemoryBlock, MemoryPassage, User
from connector_core.http import shared_client

log = logging.getLogger(__name__)

_LETTA_TIMEOUT = httpx.Timeout(2.5, connect=0.8)
_letta_skip_until = 0.0


def _letta_down() -> bool:
    return time.monotonic() < _letta_skip_until


def _mark_letta_down() -> None:
    global _letta_skip_until
    _letta_skip_until = time.monotonic() + 600


async def ensure_letta_agent(user: User) -> str | None:
    s = get_settings()
    if not s.letta_base_url:
        return None
    if user.letta_agent_id:
        return user.letta_agent_id
    if _letta_down():
        return None
    headers = {"Authorization": f"Bearer {s.letta_api_key}"} if s.letta_api_key else {}
    body = {
        "name": f"user-{user.id}",
        "description": "Personal assistant with mail and connector tools",
        "memory_blocks": [
            {"label": "human", "value": f"Name: {user.display_name}. Email: {user.email}."},
            {"label": "persona", "value": "Helpful personal assistant. Be concise. Never invent emails or events."},
        ],
        "llm_config": {"model": s.letta_agent_model or s.openai_model, "model_endpoint_type": "openai"},
    }
    try:
        resp = await shared_client().post(
            f"{s.letta_base_url}/v1/agents", json=body, headers=headers, timeout=_LETTA_TIMEOUT
        )
        if resp.status_code >= 400:
            log.warning("letta create agent failed: %s", resp.text[:200])
            _mark_letta_down()
            return None
        agent_id = resp.json().get("id") or resp.json().get("agent_id")
        return str(agent_id) if agent_id else None
    except Exception as exc:  # noqa: BLE001
        log.warning("letta unavailable: %s", exc)
        _mark_letta_down()
        return None


async def seed_letta(user_id: uuid.UUID) -> None:
    from app.db import SessionLocal

    async with SessionLocal() as db:
        user = await db.get(User, user_id)
        if user is None or user.letta_agent_id:
            return
        agent_id = await ensure_letta_agent(user)
        if agent_id:
            user.letta_agent_id = agent_id
            await db.commit()


async def load_memory(db: AsyncSession, user: User, query: str) -> str:
    s = get_settings()
    if s.letta_base_url and user.letta_agent_id and not _letta_down():
        try:
            headers = {"Authorization": f"Bearer {s.letta_api_key}"} if s.letta_api_key else {}
            resp = await shared_client().get(
                f"{s.letta_base_url}/v1/agents/{user.letta_agent_id}", headers=headers, timeout=_LETTA_TIMEOUT
            )
            if resp.status_code < 400:
                data = resp.json()
                blocks = data.get("memory", {}).get("blocks") or data.get("memory_blocks") or []
                lines = []
                for b in blocks:
                    label = b.get("label") or b.get("name")
                    value = (b.get("value") or "")[:400]
                    if value:
                        lines.append(f"{label}: {value}")
                if lines:
                    return "\n".join(lines)
        except Exception as vis_exc:  # noqa: BLE001
            log.warning("letta read failed: %s", vis_exc)
            _mark_letta_down()

    blocks = (await db.execute(select(MemoryBlock).where(MemoryBlock.user_id == user.id))).scalars().all()
    parts = [f"{b.label}: {b.value}" for b in blocks if b.value]
    try:
        vec = await embed_one(query)
        vec_lit = "[" + ",".join(f"{x:.7f}" for x in vec) + "]"
        from sqlalchemy import text

        rows = (
            await db.execute(
                text(
                    """
                    SELECT text FROM memory_passages
                    WHERE user_id = :uid AND embedding IS NOT NULL
                    ORDER BY embedding <=> CAST(:vec AS vector)
                    LIMIT 4
                    """
                ),
                {"uid": user.id, "vec": vec_lit},
            )
        ).all()
        parts.extend(f"memory: {r[0]}" for r in rows)
    except Exception:  # noqa: BLE001
        recent = (
            await db.execute(
                select(MemoryPassage)
                .where(MemoryPassage.user_id == user.id)
                .order_by(MemoryPassage.id.desc())
                .limit(3)
            )
        ).scalars().all()
        parts.extend(f"{p.kind}: {p.text}" for p in recent)
    return "\n".join(parts)[:1800]


async def remember(db: AsyncSession, user_id: uuid.UUID, kind: str, text: str) -> None:
    vec = None
    try:
        vec = await embed_one(text)
    except Exception:  # noqa: BLE001
        pass
    db.add(MemoryPassage(user_id=user_id, kind=kind, text=text[:800], embedding=vec))
    await db.commit()


async def upsert_core(db: AsyncSession, user_id: uuid.UUID, label: str, value: str) -> None:
    row = (
        await db.execute(select(MemoryBlock).where(MemoryBlock.user_id == user_id, MemoryBlock.label == label))
    ).scalar_one_or_none()
    if row is None:
        db.add(MemoryBlock(user_id=user_id, label=label, value=value[:800]))
    else:
        row.value = value[:800]
    await db.commit()
