from __future__ import annotations

import logging
import re
import time
import uuid

import httpx
from sqlalchemy import desc, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.llm import embed_one
from app.models import ChatMessage, ChatSession, MemoryBlock, MemoryPassage, User, WorkspaceAgent
from connector_core.http import shared_client

log = logging.getLogger(__name__)

_LETTA_TIMEOUT = httpx.Timeout(2.5, connect=0.8)
_letta_skip_until = 0.0
_LABEL = re.compile(r"^[A-Za-z0-9_-]{1,50}$")
CORE_LABELS = ("prefs", "people", "voice", "work", "coding")
PERSONA_LABELS = ("voice", "prefs")
STANDING_LABELS = ("people", "work", "learned")
MEMORY_BUDGET = 2400
SCOPE_AGENT = "agent"
SCOPE_SHARED = "user_shared"
STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_DELETED = "deleted"
_SECRET_LANGUAGE = re.compile(r"\b(password|passphrase|api[ _-]?key|access[ _-]?token|refresh[ _-]?token|secret|private key|recovery code)\b", re.I)
_HISTORY_INTENT = re.compile(
    r"\b(last (time|month|week|year)|remember when|we decided|what did we|previously|a month ago|"
    r"weeks ago|years ago|last time|that (decision|choice|call)|what we (chose|picked|tried))\b",
    re.I,
)
_HISTORY_STOP = {
    "the", "and", "for", "what", "did", "we", "you", "our", "last", "time", "month", "week",
    "year", "ago", "when", "remember", "previously", "that", "this", "about", "from",
    "earlier", "decide", "decided", "chose", "picked", "tried", "decision", "choice", "call",
}


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


async def _forget_letta_text(agent, snippet: str) -> None:
    s = get_settings()
    letta_id = getattr(agent, "letta_agent_id", None) if agent is not None else None
    needle = (snippet or "").strip()[:200]
    if not s.letta_base_url or not letta_id or _letta_down() or not needle:
        return
    try:
        archival = await shared_client().get(
            f"{s.letta_base_url}/v1/agents/{letta_id}/archival-memory",
            params={"query": needle, "limit": 8},
            headers=_headers(),
            timeout=_LETTA_TIMEOUT,
        )
        if archival.status_code >= 400:
            return
        items = archival.json()
        if isinstance(items, dict):
            items = items.get("passages") or items.get("results") or items.get("archival_memory") or []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            mid = item.get("id") or item.get("memory_id")
            body = str(item.get("text") or item.get("content") or "")
            if not mid or needle.lower() not in body.lower():
                continue
            await shared_client().delete(
                f"{s.letta_base_url}/v1/agents/{letta_id}/archival-memory/{mid}",
                headers=_headers(),
                timeout=_LETTA_TIMEOUT,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("letta archival delete unavailable: %s", exc)


def historical_intent(query: str) -> bool:
    return bool(_HISTORY_INTENT.search(query or ""))


def _is_active(row) -> bool:
    status = getattr(row, "status", None) or STATUS_ACTIVE
    return status == STATUS_ACTIVE


def _keyword_score(body: str, query: str) -> float:
    tokens = set(re.findall(r"[a-z0-9]{2,}", (query or "").lower()))
    words = set(re.findall(r"[a-z0-9]{2,}", (body or "").lower()))
    if not tokens or not words:
        return 0.0
    return len(tokens & words) / len(tokens)


def _rank_passages(rows: list, query: str) -> list:
    scored = []
    for row in rows:
        if not _is_active(row):
            continue
        recency = 0.3
        created = getattr(row, "created_at", None)
        if created is not None:
            try:
                age_days = max(0.0, (_now_utc() - created).total_seconds() / 86400)
                recency = max(0.05, 1.0 / (1.0 + age_days / 30.0))
            except Exception:  # noqa: BLE001
                recency = 0.3
        score = 0.55 * _keyword_score(getattr(row, "text", "") or "", query) + 0.45 * recency
        scored.append((score, row))
    scored.sort(key=lambda item: (-item[0], -(getattr(item[1], "id", 0) or 0)))
    return [row for _score, row in scored]


def _now_utc():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


async def _agent_passages(db: AsyncSession, user_id, agent_id):
    rows = (
        await db.execute(
            select(MemoryPassage).where(
                MemoryPassage.user_id == user_id,
                or_(MemoryPassage.agent_id == agent_id, MemoryPassage.scope == SCOPE_SHARED),
            )
        )
    ).scalars().all()
    return [row for row in rows if _is_active(row)]


async def _search_agent_history(db: AsyncSession, user_id, agent_id, query: str, limit: int = 3) -> list[str]:
    q = " ".join((query or "").split())[:80]
    if not q:
        return []
    tokens = [tok for tok in re.findall(r"[a-z0-9]{3,}", q.lower()) if tok not in _HISTORY_STOP][:6]
    try:
        stmt = (
            select(ChatMessage, ChatSession)
            .join(ChatSession, ChatSession.id == ChatMessage.session_id)
            .where(
                ChatMessage.user_id == user_id,
                ChatSession.user_id == user_id,
                ChatSession.agent_id == agent_id,
                ChatMessage.role.in_(("user", "assistant")),
            )
            .order_by(ChatMessage.id.desc())
            .limit(24)
        )
        if tokens:
            stmt = stmt.where(or_(*[ChatMessage.content.ilike(f"%{tok}%") for tok in tokens]))
        rows = (await db.execute(stmt)).all()
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    seen: set[str] = set()
    for message, sess in rows:
        meta = message.meta or {}
        if meta.get("in_progress") or meta.get("peer"):
            continue
        body = " ".join((message.content or "").split())[:220]
        if not body or body in seen:
            continue
        seen.add(body)
        when = ""
        stamp = getattr(message, "created_at", None)
        if stamp is not None:
            try:
                when = stamp.strftime("%Y-%m-%d")
            except Exception:  # noqa: BLE001
                when = ""
        title = (getattr(sess, "title", None) or "")[:60]
        prefix = f"{when}: " if when else ""
        suffix = f" ({title})" if title else ""
        out.append(f"{prefix}{body}{suffix}")
        if len(out) >= limit:
            break
    return out


def _assemble_memory(blocks: list, facts: list, history: list[str] | None = None) -> str:
    parts: list[str] = []
    persona = [b for b in blocks if b.label in PERSONA_LABELS and (b.value or "").strip()]
    standing_blocks = [b for b in blocks if b.label in STANDING_LABELS and (b.value or "").strip()]
    other = [b for b in blocks if b.label not in PERSONA_LABELS + STANDING_LABELS and (b.value or "").strip()]
    if persona:
        parts.append("Persona")
        parts.extend(f"{b.label}: {b.value.strip()}" for b in persona)
    if standing_blocks or facts:
        parts.append("Standing facts")
        parts.extend(f"{b.label}: {b.value.strip()}" for b in standing_blocks)
        for row in facts:
            tag = "shared" if getattr(row, "scope", SCOPE_AGENT) == SCOPE_SHARED else "fact"
            parts.append(f"- [{tag}] {row.text.strip()}")
    parts.extend(f"{b.label}: {b.value.strip()}" for b in other)
    if history:
        parts.append("From earlier chats")
        parts.extend(f"- {line}" for line in history)
    return "\n".join(parts).strip()[:MEMORY_BUDGET]


async def load_memory(db: AsyncSession, user: User, query: str, agent=None) -> str:
    """Labeled persona + facts for one workspace agent. Fail-open: empty string on error."""
    try:
        owner = await _memory_agent(db, user, agent=agent)
    except Exception as exc:  # noqa: BLE001
        log.warning("memory owner lookup failed: %s", exc)
        return ""
    try:
        blocks = (
            await db.execute(
                select(MemoryBlock).where(MemoryBlock.user_id == user.id, MemoryBlock.agent_id == owner.id)
            )
        ).scalars().all()
    except Exception as exc:  # noqa: BLE001
        log.warning("memory block load failed: %s", exc)
        blocks = []
    try:
        passages = await _agent_passages(db, user.id, owner.id)
        ranked = _rank_passages(passages, query)
        try:
            vec = await embed_one(query or "preferences")
            vec_lit = "[" + ",".join(f"{x:.7f}" for x in vec) + "]"
            hits = (
                await db.execute(
                    text(
                        """
                        SELECT id FROM memory_passages
                        WHERE user_id = :uid AND status = 'active'
                          AND (agent_id = :aid OR scope = 'user_shared')
                          AND embedding IS NOT NULL
                        ORDER BY embedding <=> CAST(:vec AS vector)
                        LIMIT 8
                        """
                    ),
                    {"uid": user.id, "aid": owner.id, "vec": vec_lit},
                )
            ).all()
            order = {row[0]: i for i, row in enumerate(hits)}
            ranked.sort(key=lambda row: (order.get(row.id, 99), -_keyword_score(row.text or "", query)))
        except Exception:  # noqa: BLE001
            pass
        facts = ranked[:8]
    except Exception as vis_exc:  # noqa: BLE001
        log.warning("memory passage load failed: %s", vis_exc)
        facts = []
    history: list[str] = []
    if historical_intent(query or ""):
        history = await _search_agent_history(db, user.id, owner.id, query)
    assembled = _assemble_memory(list(blocks), facts, history)
    if getattr(owner, "letta_agent_id", None) and not _letta_down():
        letta_text = await _read_letta(owner, query)
        if letta_text:
            extra = []
            blob = assembled.lower()
            for line in letta_text.splitlines():
                stripped = line.strip()
                if not stripped or stripped.lower().startswith("memory:"):
                    continue
                if stripped.lower() in blob:
                    continue
                extra.append(stripped)
            if extra:
                assembled = (assembled + ("\n" if assembled else "") + "\n".join(extra)).strip()[:MEMORY_BUDGET]
            elif not assembled:
                assembled = letta_text[:MEMORY_BUDGET]
    return assembled


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


async def remember(
    db: AsyncSession,
    user_id: uuid.UUID,
    kind: str,
    text: str,
    agent_id=None,
    scope: str = SCOPE_AGENT,
    source_session_id=None,
    source_message_id=None,
    supersedes_id=None,
) -> MemoryPassage | None:
    user = await db.get(User, user_id)
    if user is None:
        return None
    owner = await _memory_agent(db, user, agent_id=agent_id)
    if (scope or SCOPE_AGENT) == SCOPE_SHARED:
        from app.agents.service import ensure_default_agent

        owner = await ensure_default_agent(db, user)
        scope = SCOPE_SHARED
    else:
        scope = SCOPE_AGENT
    await _ensure_saved_letta(db, user, owner)
    vec = None
    try:
        vec = await embed_one(text)
    except Exception:  # noqa: BLE001
        pass
    if supersedes_id:
        prior = await db.get(MemoryPassage, supersedes_id)
        if prior is not None and prior.user_id == user_id:
            prior.status = STATUS_SUPERSEDED
    row = MemoryPassage(
        user_id=user_id,
        agent_id=owner.id,
        kind=kind,
        text=text[:800],
        embedding=vec,
        scope=scope,
        status=STATUS_ACTIVE,
        source_session_id=source_session_id,
        source_message_id=source_message_id,
        supersedes_id=supersedes_id,
    )
    db.add(row)
    await db.commit()
    await _write_letta_passage(owner, text[:800])
    return row


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
    await remember(
        ctx.db,
        ctx.user.id,
        "archival",
        body[:800],
        getattr(owner, "id", None),
        scope=SCOPE_SHARED if shared else SCOPE_AGENT,
    )
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
    try:
        candidates = await _agent_passages(ctx.db, ctx.user.id, owner.id)
    except Exception:  # noqa: BLE001
        candidates = []
    if q:
        ranked = _rank_passages(candidates, q)
        if not ranked:
            needle = q[:80].lower()
            ranked = [row for row in candidates if needle in (row.text or "").lower()][:6]
        rows = ranked[:6]
    else:
        rows = sorted(candidates, key=lambda row: getattr(row, "id", 0) or 0, reverse=True)[:6]
    passages = [
        {
            "kind": r.kind,
            "id": r.id,
            "text": r.text,
            "scope": getattr(r, "scope", SCOPE_AGENT),
        }
        for r in rows
    ]
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
            if row is not None and row.user_id == ctx.user.id and (
                row.agent_id == owner.id or getattr(row, "scope", "") == SCOPE_SHARED
            ):
                row.status = STATUS_DELETED
                removed.append(f"passage:{pid}")
                await _forget_letta_text(owner, row.text or "")
    snippet = (text or "").strip()
    if snippet and not memory_id and not tag:
        rows = (
            await ctx.db.execute(
                select(MemoryPassage).where(
                    MemoryPassage.user_id == ctx.user.id,
                    or_(MemoryPassage.agent_id == owner.id, MemoryPassage.scope == SCOPE_SHARED),
                    MemoryPassage.text.ilike(f"%{snippet[:80]}%"),
                )
            )
        ).scalars().all()
        for row in rows[:8]:
            if getattr(row, "status", STATUS_ACTIVE) == STATUS_DELETED:
                continue
            if snippet.lower() not in (row.text or "").lower():
                continue
            row.status = STATUS_DELETED
            removed.append(f"passage:{row.id}")
            await _forget_letta_text(owner, row.text or "")
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
            .where(
                MemoryPassage.user_id == user.id,
                or_(MemoryPassage.agent_id == agent.id, MemoryPassage.scope == SCOPE_SHARED),
            )
            .order_by(desc(MemoryPassage.id))
            .limit(80)
        )
    ).scalars().all()
    active = [row for row in passages if _is_active(row)]
    private = [row for row in active if getattr(row, "scope", SCOPE_AGENT) != SCOPE_SHARED]
    shared = [row for row in active if getattr(row, "scope", SCOPE_AGENT) == SCOPE_SHARED]
    persona = [{"label": row.label, "value": row.value} for row in blocks if row.label in PERSONA_LABELS and row.value]
    def _row(item):
        created = getattr(item, "created_at", None)
        return {
            "id": item.id,
            "kind": item.kind,
            "text": item.text,
            "scope": getattr(item, "scope", SCOPE_AGENT),
            "created_at": created.isoformat() if created is not None else "",
        }

    return {
        "agent_id": str(agent.id),
        "agent_name": agent.name,
        "persona": persona,
        "core": [
            {"label": row.label, "value": row.value}
            for row in blocks
            if row.label not in PERSONA_LABELS and row.value
        ],
        "private_facts": [_row(item) for item in private[:40]],
        "shared_facts": [_row(item) for item in shared[:40]],
        "recent_work": [_row(item) for item in private[:50]],
    }
