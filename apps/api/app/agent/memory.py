from __future__ import annotations

import logging
import re
import time
import uuid

import httpx
from sqlalchemy import desc, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.llm import embed_one
from app.models import ChatMessage, MemoryBlock, MemoryPassage, User, WorkspaceAgent
from connector_core.http import shared_client

log = logging.getLogger(__name__)

_LETTA_TIMEOUT = httpx.Timeout(2.5, connect=0.8)
_letta_skip_until = 0.0
_LABEL = re.compile(r"^[A-Za-z0-9_-]{1,50}$")
CORE_LABELS = ("prefs", "people", "voice", "work", "coding")
_SECRET_LANGUAGE = re.compile(r"\b(password|passphrase|api[ _-]?key|access[ _-]?token|refresh[ _-]?token|secret|private key|recovery code)\b", re.I)


def _letta_down() -> bool:
    return time.monotonic() < _letta_skip_until


def _mark_letta_down() -> None:
    global _letta_skip_until
    _letta_skip_until = time.monotonic() + 600


def _headers() -> dict:
    key = get_settings().letta_api_key
    return {"Authorization": f"Bearer {key}"} if key else {}


async def ensure_letta_agent(user: User, workspace_agent=None) -> str | None:
    """Return the Letta id for this workspace agent.

    Called with only ``user``, this keeps the legacy one-agent path (Chief / login seed).
    A custom workspace agent never reuses ``user.letta_agent_id`` — that id belongs to Chief.
    """
    s = get_settings()
    if not s.letta_base_url:
        return None
    if workspace_agent is not None:
        existing = getattr(workspace_agent, "letta_agent_id", None)
        if existing:
            return str(existing)
        if getattr(workspace_agent, "kind", None) == "default" and user.letta_agent_id:
            return user.letta_agent_id
    elif user.letta_agent_id:
        return user.letta_agent_id
    if _letta_down():
        return None
    if workspace_agent is None:
        name = f"user-{user.id}"
        description = "Personal assistant with mail and connector tools"
        persona = "Helpful personal assistant. Be concise. Never invent emails or events."
    else:
        name = f"ws-{getattr(workspace_agent, 'id', user.id)}"
        title = getattr(workspace_agent, "name", None) or "agent"
        description = f"{title} for user {user.id}"
        persona = (getattr(workspace_agent, "instructions", None) or "").strip()
        if not persona:
            persona = "Helpful personal assistant. Be concise. Never invent emails or events."
        persona = persona[:2000]
    body = {
        "name": name,
        "description": description,
        "memory_blocks": [
            {"label": "human", "value": f"Name: {user.display_name}. Email: {user.email}."},
            {"label": "persona", "value": persona},
        ],
        "llm_config": {"model": s.letta_agent_model or s.openai_model, "model_endpoint_type": "openai"},
    }
    try:
        resp = await shared_client().post(
            f"{s.letta_base_url}/v1/agents", json=body, headers=_headers(), timeout=_LETTA_TIMEOUT
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
    """Create Chief's Letta agent and mirror its id onto ``users.letta_agent_id``."""
    from app.agents.service import ensure_default_agent
    from app.db import SessionLocal

    async with SessionLocal() as db:
        user = await db.get(User, user_id)
        if user is None:
            return
        chief = await ensure_default_agent(db, user)
        if not chief.letta_agent_id and user.letta_agent_id:
            chief.letta_agent_id = user.letta_agent_id
        if chief.letta_agent_id:
            if not user.letta_agent_id:
                user.letta_agent_id = chief.letta_agent_id
            await db.commit()
            return
        agent_id = await ensure_letta_agent(user, chief)
        if agent_id:
            chief.letta_agent_id = agent_id
            user.letta_agent_id = agent_id
            await db.commit()


async def forget_letta_agent(agent, user=None) -> None:
    """Best-effort remote delete. Never deletes the id stored on the user row (Chief's)."""
    letta_id = getattr(agent, "letta_agent_id", None)
    if not letta_id:
        return
    shared = getattr(user, "letta_agent_id", None) if user is not None else None
    if shared and shared == letta_id:
        return
    s = get_settings()
    if not s.letta_base_url or _letta_down():
        return
    try:
        resp = await shared_client().delete(
            f"{s.letta_base_url}/v1/agents/{letta_id}", headers=_headers(), timeout=_LETTA_TIMEOUT
        )
        if resp.status_code >= 400 and resp.status_code != 404:
            log.warning("letta delete agent failed: %s", resp.text[:200])
    except Exception as exc:  # noqa: BLE001
        log.warning("letta delete unavailable: %s", exc)


async def _memory_agent(db: AsyncSession, user: User, agent_id=None, agent=None):
    if agent is not None and getattr(agent, "id", None):
        owner = getattr(agent, "user_id", None)
        if owner is None or owner == user.id:
            return agent
    if agent_id:
        try:
            uid = agent_id if isinstance(agent_id, uuid.UUID) else uuid.UUID(str(agent_id))
        except (TypeError, ValueError):
            uid = None
        if uid is not None:
            row = await db.get(WorkspaceAgent, uid)
            if row is not None and row.user_id == user.id:
                return row
    from app.agents.service import ensure_default_agent

    return await ensure_default_agent(db, user)


async def _ensure_saved_letta(db: AsyncSession, user: User, agent) -> str | None:
    if agent is None:
        return None
    current = getattr(agent, "letta_agent_id", None)
    dirty = False
    if not current and getattr(agent, "kind", None) == "default" and user.letta_agent_id:
        agent.letta_agent_id = user.letta_agent_id
        current = agent.letta_agent_id
        dirty = True
    if current:
        if getattr(agent, "kind", None) == "default" and not user.letta_agent_id:
            user.letta_agent_id = current
            dirty = True
        if dirty:
            await db.commit()
        return str(current)
    created = await ensure_letta_agent(user, agent)
    if not created:
        return None
    agent.letta_agent_id = created
    if getattr(agent, "kind", None) == "default":
        user.letta_agent_id = created
    await db.commit()
    return created


def _block_lines(data: dict) -> list[str]:
    blocks = data.get("memory", {}).get("blocks") or data.get("memory_blocks") or []
    lines = []
    for block in blocks:
        label = block.get("label") or block.get("name")
        value = (block.get("value") or "")[:400]
        if value:
            lines.append(f"{label}: {value}")
    return lines


def _passage_lines(data) -> list[str]:
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("passages") or data.get("results") or data.get("archival_memory") or []
    else:
        items = []
    lines = []
    for item in items:
        if isinstance(item, str) and item.strip():
            lines.append(item.strip())
        elif isinstance(item, dict):
            value = item.get("text") or item.get("content") or ""
            if value:
                lines.append(str(value))
    return lines


async def _read_letta(agent, query: str) -> str | None:
    s = get_settings()
    letta_id = getattr(agent, "letta_agent_id", None) if agent is not None else None
    if not s.letta_base_url or not letta_id or _letta_down():
        return None
    try:
        resp = await shared_client().get(
            f"{s.letta_base_url}/v1/agents/{letta_id}", headers=_headers(), timeout=_LETTA_TIMEOUT
        )
        if resp.status_code >= 400:
            log.warning("letta read failed: %s", resp.text[:200])
            return None
        lines = _block_lines(resp.json())
        try:
            archival = await shared_client().get(
                f"{s.letta_base_url}/v1/agents/{letta_id}/archival-memory",
                params={"query": (query or "")[:200], "limit": 4},
                headers=_headers(),
                timeout=_LETTA_TIMEOUT,
            )
            if archival.status_code < 400:
                for value in _passage_lines(archival.json())[:4]:
                    lines.append(f"memory: {value[:400]}")
        except Exception as exc:  # noqa: BLE001
            log.warning("letta archival read failed: %s", exc)
        return "\n".join(lines) if lines else None
    except Exception as exc:  # noqa: BLE001
        log.warning("letta read failed: %s", exc)
        _mark_letta_down()
        return None


async def _write_letta_passage(agent, text: str) -> None:
    s = get_settings()
    letta_id = getattr(agent, "letta_agent_id", None) if agent is not None else None
    if not s.letta_base_url or not letta_id or _letta_down() or not text:
        return
    try:
        resp = await shared_client().post(
            f"{s.letta_base_url}/v1/agents/{letta_id}/archival-memory",
            json={"text": text[:800]},
            headers=_headers(),
            timeout=_LETTA_TIMEOUT,
        )
        if resp.status_code >= 400:
            log.warning("letta archival write failed: %s", resp.text[:200])
    except Exception as exc:  # noqa: BLE001
        log.warning("letta archival write unavailable: %s", exc)


async def _write_letta_block(agent, label: str, value: str) -> None:
    s = get_settings()
    letta_id = getattr(agent, "letta_agent_id", None) if agent is not None else None
    if not s.letta_base_url or not letta_id or _letta_down() or not _LABEL.match(label or ""):
        return
    try:
        resp = await shared_client().patch(
            f"{s.letta_base_url}/v1/agents/{letta_id}/core-memory/blocks/{label}",
            json={"value": (value or "")[:800]},
            headers=_headers(),
            timeout=_LETTA_TIMEOUT,
        )
        if resp.status_code == 404:
            resp = await shared_client().post(
                f"{s.letta_base_url}/v1/agents/{letta_id}/core-memory/blocks",
                json={"label": label, "value": (value or "")[:800]},
                headers=_headers(),
                timeout=_LETTA_TIMEOUT,
            )
        if resp.status_code >= 400:
            log.warning("letta block write failed: %s", resp.text[:200])
    except Exception as exc:  # noqa: BLE001
        log.warning("letta block write unavailable: %s", exc)


async def _load_fallback(db: AsyncSession, user_id, agent_id, query: str) -> str:
    blocks = (
        await db.execute(
            select(MemoryBlock).where(MemoryBlock.user_id == user_id, MemoryBlock.agent_id == agent_id)
        )
    ).scalars().all()
    parts = [f"{b.label}: {b.value}" for b in blocks if b.value]
    try:
        vec = await embed_one(query)
        vec_lit = "[" + ",".join(f"{x:.7f}" for x in vec) + "]"
        rows = (
            await db.execute(
                text(
                    """
                    SELECT text FROM memory_passages
                    WHERE user_id = :uid AND agent_id = :aid AND embedding IS NOT NULL
                    ORDER BY embedding <=> CAST(:vec AS vector)
                    LIMIT 4
                    """
                ),
                {"uid": user_id, "aid": agent_id, "vec": vec_lit},
            )
        ).all()
        parts.extend(f"memory: {r[0]}" for r in rows)
    except Exception:  # noqa: BLE001
        recent = (
            await db.execute(
                select(MemoryPassage)
                .where(MemoryPassage.user_id == user_id, MemoryPassage.agent_id == agent_id)
                .order_by(MemoryPassage.id.desc())
                .limit(3)
            )
        ).scalars().all()
        parts.extend(f"{p.kind}: {p.text}" for p in recent)
    return "\n".join(parts)[:1800]


async def load_memory(db: AsyncSession, user: User, query: str, agent=None) -> str:
    """Memory for one workspace agent. Group chats pass the speaker; teammates pass themselves."""
    owner = await _memory_agent(db, user, agent=agent)
    await _ensure_saved_letta(db, user, owner)
    letta_text = await _read_letta(owner, query)
    fallback = await _load_fallback(db, user.id, owner.id, query)
    if not letta_text:
        return fallback
    if not fallback:
        return letta_text[:1800]
    lines = [line for line in letta_text.splitlines() if line.strip()]
    seen = set(lines)
    for line in fallback.splitlines():
        if line.strip() and line not in seen and line not in letta_text:
            lines.append(line)
            seen.add(line)
    return "\n".join(lines)[:1800]


async def read_core(db: AsyncSession, user: User, label: str, agent=None) -> str:
    """One core block. With no agent, this is Chief's block (inbox voice)."""
    owner = await _memory_agent(db, user, agent=agent)
    row = (
        await db.execute(
            select(MemoryBlock).where(
                MemoryBlock.user_id == user.id,
                MemoryBlock.agent_id == owner.id,
                MemoryBlock.label == label,
            )
        )
    ).scalar_one_or_none()
    return (row.value if row else "") or ""


async def remember(db: AsyncSession, user_id: uuid.UUID, kind: str, text: str, agent_id=None) -> None:
    user = await db.get(User, user_id)
    if user is None:
        return
    owner = await _memory_agent(db, user, agent_id=agent_id)
    await _ensure_saved_letta(db, user, owner)
    vec = None
    try:
        vec = await embed_one(text)
    except Exception:  # noqa: BLE001
        pass
    db.add(MemoryPassage(user_id=user_id, agent_id=owner.id, kind=kind, text=text[:800], embedding=vec))
    await db.commit()
    await _write_letta_passage(owner, text[:800])


async def upsert_core(db: AsyncSession, user_id: uuid.UUID, label: str, value: str, agent_id) -> None:
    user = await db.get(User, user_id)
    if user is None:
        return
    owner = await _memory_agent(db, user, agent_id=agent_id)
    row = (
        await db.execute(
            select(MemoryBlock).where(
                MemoryBlock.user_id == user_id,
                MemoryBlock.agent_id == owner.id,
                MemoryBlock.label == label,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        db.add(MemoryBlock(user_id=user_id, agent_id=owner.id, label=label, value=value[:800]))
    else:
        row.value = value[:800]
    await _ensure_saved_letta(db, user, owner)
    await db.commit()
    await _write_letta_block(owner, label, value[:800])


def _need_db(ctx) -> dict | None:
    if ctx.db is None:
        return {"error": "Memory needs a workspace."}
    return None


async def remember_fact(ctx, text: str, label: str = "", shared: bool = False) -> dict:
    missing = _need_db(ctx)
    if missing:
        return missing
    body = " ".join((text or "").split())
    if not body:
        return {"error": "text is required"}
    if _SECRET_LANGUAGE.search(body):
        return {"error": "I can’t store passwords, keys, tokens, or recovery codes in memory."}
    tag = (label or "").strip().lower()
    owner = ctx.agent
    if shared:
        from app.agents.service import ensure_default_agent

        owner = await ensure_default_agent(ctx.db, ctx.user)
    if tag in CORE_LABELS:
        existing = await read_core(ctx.db, ctx.user, tag, agent=owner)
        merged = (existing.rstrip() + "\n" + body).strip() if existing else body
        await upsert_core(ctx.db, ctx.user.id, tag, merged[:800], getattr(owner, "id", None))
        return {"ok": True, "kind": "block", "label": tag, "shared": bool(shared), "value": merged[:800]}
    await remember(ctx.db, ctx.user.id, "archival", body[:800], getattr(owner, "id", None))
    return {"ok": True, "kind": "passage", "shared": bool(shared), "text": body[:800]}


async def recall_memory(ctx, query: str = "", label: str = "") -> dict:
    missing = _need_db(ctx)
    if missing:
        return missing
    owner = await _memory_agent(ctx.db, ctx.user, agent=ctx.agent)
    tag = (label or "").strip().lower()
    blocks = (
        await ctx.db.execute(
            select(MemoryBlock).where(MemoryBlock.user_id == ctx.user.id, MemoryBlock.agent_id == owner.id)
        )
    ).scalars().all()
    if tag:
        blocks = [b for b in blocks if b.label == tag]
    block_rows = [{"kind": "block", "label": b.label, "text": b.value} for b in blocks if b.value]
    q = (query or "").strip()
    passages: list[dict] = []
    if q:
        try:
            vec = await embed_one(q)
            vec_lit = "[" + ",".join(f"{x:.7f}" for x in vec) + "]"
            rows = (
                await ctx.db.execute(
                    text(
                        """
                        SELECT id, kind, text FROM memory_passages
                        WHERE user_id = :uid AND agent_id = :aid AND embedding IS NOT NULL
                        ORDER BY embedding <=> CAST(:vec AS vector)
                        LIMIT 6
                        """
                    ),
                    {"uid": ctx.user.id, "aid": owner.id, "vec": vec_lit},
                )
            ).all()
            passages = [{"kind": r[1], "id": r[0], "text": r[2]} for r in rows]
        except Exception:  # noqa: BLE001
            needle = f"%{q[:80]}%"
            rows = (
                await ctx.db.execute(
                    select(MemoryPassage)
                    .where(
                        MemoryPassage.user_id == ctx.user.id,
                        MemoryPassage.agent_id == owner.id,
                        MemoryPassage.text.ilike(needle),
                    )
                    .order_by(MemoryPassage.id.desc())
                    .limit(6)
                )
            ).scalars().all()
            passages = [{"kind": r.kind, "id": r.id, "text": r.text} for r in rows]
    else:
        rows = (
            await ctx.db.execute(
                select(MemoryPassage)
                .where(MemoryPassage.user_id == ctx.user.id, MemoryPassage.agent_id == owner.id)
                .order_by(MemoryPassage.id.desc())
                .limit(6)
            )
        ).scalars().all()
        passages = [{"kind": r.kind, "id": r.id, "text": r.text} for r in rows]
    return {"blocks": block_rows, "passages": passages}


async def forget_memory(ctx, memory_id: str = "", label: str = "", text: str = "") -> dict:
    missing = _need_db(ctx)
    if missing:
        return missing
    owner = await _memory_agent(ctx.db, ctx.user, agent=ctx.agent)
    removed: list[str] = []
    tag = (label or "").strip().lower()
    if tag:
        row = (
            await ctx.db.execute(
                select(MemoryBlock).where(
                    MemoryBlock.user_id == ctx.user.id,
                    MemoryBlock.agent_id == owner.id,
                    MemoryBlock.label == tag,
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            needle = (text or "").strip()
            if needle and needle.lower() in (row.value or "").lower():
                kept = "\n".join(line for line in row.value.splitlines() if needle.lower() not in line.lower())
                row.value = kept[:800]
                removed.append(f"block:{tag}")
            else:
                await ctx.db.delete(row)
                removed.append(f"block:{tag}")
    if memory_id:
        try:
            pid = int(str(memory_id).lstrip("#"))
        except ValueError:
            pid = None
        if pid is not None:
            row = await ctx.db.get(MemoryPassage, pid)
            if row is not None and row.user_id == ctx.user.id and row.agent_id == owner.id:
                await ctx.db.delete(row)
                removed.append(f"passage:{pid}")
    snippet = (text or "").strip()
    if snippet and not memory_id and not tag:
        rows = (
            await ctx.db.execute(
                select(MemoryPassage)
                .where(
                    MemoryPassage.user_id == ctx.user.id,
                    MemoryPassage.agent_id == owner.id,
                    MemoryPassage.text.ilike(f"%{snippet[:80]}%"),
                )
                .limit(8)
            )
        ).scalars().all()
        for row in rows:
            await ctx.db.delete(row)
            removed.append(f"passage:{row.id}")
    if not removed:
        return {"error": "Nothing matched to forget. Pass memory_id, label, or text."}
    await ctx.db.commit()
    return {"ok": True, "removed": removed}


async def search_history(ctx, query: str, limit: int = 12) -> dict:
    missing = _need_db(ctx)
    if missing:
        return missing
    q = (query or "").strip()
    if not q:
        return {"error": "query is required"}
    stmt = (
        select(ChatMessage)
        .where(ChatMessage.user_id == ctx.user.id, ChatMessage.content.ilike(f"%{q[:80]}%"))
        .order_by(ChatMessage.id.desc())
        .limit(max(1, min(int(limit or 12), 30)))
    )
    if ctx.session_id:
        stmt = stmt.where(ChatMessage.session_id == ctx.session_id)
    rows = (await ctx.db.execute(stmt)).scalars().all()
    return {
        "query": q,
        "messages": [
            {"id": r.id, "role": r.role, "text": (r.content or "")[:400], "session_id": str(r.session_id)} for r in rows
        ],
    }


async def read_history(ctx, limit: int = 8) -> dict:
    missing = _need_db(ctx)
    if missing:
        return missing
    if ctx.session_id is None:
        return {"error": "No chat session is active."}
    rows = (
        await ctx.db.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == ctx.session_id)
            .order_by(ChatMessage.id.desc())
            .limit(max(1, min(int(limit or 8), 40)))
        )
    ).scalars().all()
    rows = list(reversed(rows))
    return {
        "messages": [{"id": r.id, "role": r.role, "text": (r.content or "")[:600]} for r in rows],
    }


async def learn_for_agent(db: AsyncSession, user: User, agent, category: str, fact: str) -> dict:
    """Store a short, durable preference in this agent's own memory block."""
    if db is None or agent is None or getattr(agent, "user_id", None) != user.id:
        return {"saved": False, "error": "Choose one of your agents first."}
    clean_fact = " ".join((fact or "").split())[:400]
    clean_category = re.sub(r"[^a-z0-9_-]", "", (category or "preference").lower())[:30] or "preference"
    if not clean_fact:
        return {"saved": False, "error": "Add a specific preference or working detail to remember."}
    if _SECRET_LANGUAGE.search(clean_fact):
        return {"saved": False, "error": "I can’t store passwords, keys, tokens, or recovery codes in memory."}
    prior = await read_core(db, user, "learned", agent)
    line = f"- {clean_category}: {clean_fact}"
    lines = [part for part in (prior or "").splitlines() if part.strip()]
    if line.casefold() not in {part.casefold() for part in lines}:
        lines.append(line)
    value = "\n".join(lines[-12:])[:780]
    await upsert_core(db, user.id, "learned", value, agent.id)
    return {"saved": True, "agent": agent.name, "memory": value}


async def list_agent_memory(db: AsyncSession, user: User, agent_id: uuid.UUID) -> dict:
    """Return only the selected user's selected agent memory for review."""
    from app.agents.service import get_agent

    agent = await get_agent(db, user, agent_id)
    blocks = (
        await db.execute(
            select(MemoryBlock).where(
                MemoryBlock.user_id == user.id,
                MemoryBlock.agent_id == agent.id,
            ).order_by(MemoryBlock.label)
        )
    ).scalars().all()
    passages = (
        await db.execute(
            select(MemoryPassage)
            .where(MemoryPassage.user_id == user.id, MemoryPassage.agent_id == agent.id)
            .order_by(desc(MemoryPassage.id))
            .limit(50)
        )
    ).scalars().all()
    return {
        "agent_id": str(agent.id),
        "agent_name": agent.name,
        "core": [{"label": row.label, "value": row.value} for row in blocks],
        "recent_work": [
            {"id": row.id, "kind": row.kind, "text": row.text, "created_at": row.created_at.isoformat()}
            for row in passages
        ],
    }
