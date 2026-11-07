from __future__ import annotations

import logging
import re
import time
import uuid

import httpx
from sqlalchemy import desc, func, or_, select, text
from sqlalchemy.exc import IntegrityError
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
PERSONA_LABELS = ("voice", "prefs", "voice_colleagues", "voice_external")
STANDING_LABELS = ("people", "work", "learned")
SCOPE_AGENT = "agent"  # a note about how one agent works: read by that agent only
SCOPE_USER = "user"  # a fact about the user's world: read by all of the user's agents
SCOPE_SHARED = "user_shared"  # old name for SCOPE_USER, still read as user-level
USER_SCOPES = (SCOPE_USER, SCOPE_SHARED)
# Core blocks about the user (how they like to be answered, what was learned about them, their people and work) are
# one copy per user, kept on the user's default agent and read by every agent. Other labels ("coding") stay per agent.
USER_LABELS = ("voice", "prefs", "learned", "people", "work", "voice_colleagues", "voice_external")
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
            json={"text": text},
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
            json={"value": value or ""},
            headers=_headers(),
            timeout=_LETTA_TIMEOUT,
        )
        if resp.status_code == 404:
            resp = await shared_client().post(
                f"{s.letta_base_url}/v1/agents/{letta_id}/core-memory/blocks",
                json={"label": label, "value": value or ""},
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


def _recency(row) -> float:
    created = getattr(row, "created_at", None)
    if created is None:
        return 0.3
    try:
        age_days = max(0.0, (_now_utc() - created).total_seconds() / 86400)
        return max(0.05, 1.0 / (1.0 + age_days / 30.0))
    except Exception:  # noqa: BLE001
        return 0.3


def _now_utc():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


# Each candidate pool (nearest by embedding, best full-text match, newest) is capped, so recall reads a bounded
# number of rows however large an agent's memory grows.
FACT_POOL = 16
_TS_STOP = {"the", "and", "for", "you", "what", "with", "this", "that", "are", "was", "is", "my", "me", "to", "of", "in", "on", "it", "an", "do", "can"}
_FACT_SCOPE_SQL = "user_id = :uid AND status = 'active' AND (scope IN ('user', 'user_shared') OR agent_id = :aid)"


def _tsquery(query: str) -> str:
    """An OR query of the distinct words in ``query``. Words are [a-z0-9] only, so it is always valid tsquery."""
    tokens: list[str] = []
    for tok in re.findall(r"[a-z0-9]{2,}", (query or "").lower()):
        if tok not in _TS_STOP and tok not in tokens:
            tokens.append(tok)
    return " | ".join(tokens[:8])


def _visible_fact(row, user_id, agent_id) -> bool:
    return (
        _is_active(row)
        and row.user_id == user_id
        and (getattr(row, "scope", SCOPE_AGENT) in USER_SCOPES or row.agent_id == agent_id)
    )


async def _attach_subject_names(db: AsyncSession, user_id, rows: list) -> None:
    """Name the person or organisation each fact is about (rows about the user themselves have none)."""
    ids = {getattr(r, "subject_entity_id", None) for r in rows}
    ids.discard(None)
    if not ids:
        return
    try:
        from app.memory_engine.entities import names_for

        names = await names_for(db, user_id, ids)
    except Exception:  # noqa: BLE001
        log.info("entity names skipped", exc_info=True)
        return
    for row in rows:
        sid = getattr(row, "subject_entity_id", None)
        if sid in names:
            row.subject_name = names[sid]


async def _newest_facts(db: AsyncSession, user_id, agent_id, limit: int) -> list:
    rows = (
        await db.execute(
            select(MemoryPassage)
            .where(
                MemoryPassage.user_id == user_id,
                MemoryPassage.status == STATUS_ACTIVE,
                or_(MemoryPassage.scope.in_(USER_SCOPES), MemoryPassage.agent_id == agent_id),
            )
            .order_by(MemoryPassage.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [row for row in rows if _visible_fact(row, user_id, agent_id)]


async def _ranked_facts(db: AsyncSession, user_id, agent_id, query: str, limit: int = 8) -> list:
    """The active facts (this agent's and user-shared ones) most relevant to ``query``, best first.

    Postgres picks three bounded candidate pools: nearest by embedding, best full-text match on ``search_tsv``, and
    newest. Only those rows are loaded and blended: embedding rank first, then word overlap and recency."""
    params = {"uid": user_id, "aid": agent_id, "lim": FACT_POOL}
    q = (query or "").strip()
    vec_rank: dict[int, int] = {}
    try:
        vec = await embed_one(q or "preferences")
        vec_lit = "[" + ",".join(f"{x:.7f}" for x in vec) + "]"
        hits = (
            await db.execute(
                text(
                    f"SELECT id FROM memory_passages WHERE {_FACT_SCOPE_SQL} AND embedding IS NOT NULL "
                    "ORDER BY embedding <=> CAST(:vec AS vector) LIMIT :lim"
                ),
                {**params, "vec": vec_lit},
            )
        ).all()
        vec_rank = {row[0]: i for i, row in enumerate(hits)}
    except Exception:  # noqa: BLE001 - keyword and recency still rank
        log.info("memory vector recall skipped", exc_info=True)
    keyword_ids: list[int] = []
    tsq = _tsquery(q)
    if tsq:
        try:
            hits = (
                await db.execute(
                    text(
                        f"SELECT id FROM memory_passages WHERE {_FACT_SCOPE_SQL} "
                        "AND search_tsv @@ to_tsquery('simple', :tsq) "
                        "ORDER BY ts_rank(search_tsv, to_tsquery('simple', :tsq)) DESC, id DESC LIMIT :lim"
                    ),
                    {**params, "tsq": tsq},
                )
            ).all()
            keyword_ids = [row[0] for row in hits]
        except Exception:  # noqa: BLE001
            log.info("memory keyword recall skipped", exc_info=True)
    newest = await _newest_facts(db, user_id, agent_id, max(limit, 4))
    wanted = [i for i in dict.fromkeys([*vec_rank, *keyword_ids]) if i not in {r.id for r in newest}]
    rows = list(newest)
    if wanted:
        loaded = (await db.execute(select(MemoryPassage).where(MemoryPassage.id.in_(wanted)))).scalars().all()
        rows.extend(row for row in loaded if _visible_fact(row, user_id, agent_id))

    def score(row) -> float:
        rank = vec_rank.get(row.id)
        vector = 1.0 / (1.0 + rank) if rank is not None else 0.0  # reciprocal rank: the nearest match leads
        confidence = float(getattr(row, "confidence", None) or 0.8)
        return 0.45 * vector + 0.25 * _keyword_score(row.text or "", q) + 0.15 * _recency(row) + 0.15 * confidence

    rows.sort(key=lambda row: (-score(row), -(row.id or 0)))
    return rows[:limit]


# Core blocks are rewritten, never appended to: the newest note wins a conflict. (max_chars, max_lines) caps the
# block's size; None means no cap. "learned" (what remember_for_me saves) is deliberately uncapped.
BLOCK_LIMITS: dict[str, tuple[int | None, int | None]] = {"voice": (900, 10), "prefs": (900, 10), "learned": (None, None)}
DEFAULT_BLOCK_LIMIT = (1500, 16)
_BULLET = re.compile(r"^\s*(?:[-•*]|\d{1,2}[.)])\s*")


def _block_note_lines(value: str) -> list[str]:
    return [_BULLET.sub("", line).strip() for line in (value or "").splitlines() if _BULLET.sub("", line).strip()]


def _fit_block(lines: list[str], max_chars: int | None, max_lines: int | None) -> str:
    """Drop repeats (keeping the later copy), then keep the newest lines that fit, in their original order."""
    max_chars = max_chars if max_chars is not None else float("inf")
    max_lines = max_lines if max_lines is not None else float("inf")
    seen: set[str] = set()
    newest_first: list[str] = []
    for line in reversed(lines):
        key = line.lower()
        if line and key not in seen:
            seen.add(key)
            newest_first.append(line)
    kept: list[str] = []
    size = 0
    for line in newest_first:
        if len(kept) >= max_lines or size + len(line) + 1 > max_chars:
            break
        kept.append(line)
        size += len(line) + 1
    return "\n".join(reversed(kept))


async def _rewrite_block(label: str, lines: list[str], max_chars: int | None, max_lines: int | None) -> str:
    from app.llm import chat_complete, small_model

    notes = "\n".join(f"{i + 1}. {line}" for i, line in enumerate(lines))
    size = (
        f"at most {max_lines} short lines and {max_chars} characters in total"
        if max_chars is not None and max_lines is not None
        else "short lines, as many as are needed"
    )
    prompt = (
        f"These notes are an assistant's '{label}' memory about one user, oldest first. Rewrite them as {size}. "
        "When notes conflict, the later one wins, but only on the point they disagree about: if a note is partly "
        "out of date, keep every part of it that still holds (for example, a new city replaces the old city but "
        "not the job mentioned in the same note). "
        "Merge notes that say the same thing. Keep every other note that still holds. Do not add anything new. "
        "Return only the lines, one per line, with no numbers or bullets.\n\n" + notes
    )
    # An uncapped block can be long: leave the model room to return all of it.
    budget = max(400, len(notes) // 2 + 200)
    try:
        resp = await chat_complete(
            [{"role": "user", "content": prompt}], max_tokens=budget, temperature=0, model=small_model()
        )
        raw = resp.choices[0].message.content or ""
    except Exception:  # noqa: BLE001 - the trimmed block is the fallback
        log.info("memory block rewrite failed", exc_info=True)
        return ""
    out = _block_note_lines(raw)
    if not out or any(_SECRET_LANGUAGE.search(line) for line in out):
        return ""
    return _fit_block(out, max_chars, max_lines)


async def merge_block(label: str, prior: str, addition: str) -> tuple[str, bool]:
    """Fold ``addition`` into a core block. Returns the new value and whether it changed."""
    new = " ".join((addition or "").split())
    old = (prior or "").strip()
    max_chars, max_lines = BLOCK_LIMITS.get(label, DEFAULT_BLOCK_LIMIT)
    if not new or new.lower() in old.lower():
        return old, False
    if not old:
        return (new[:max_chars] if max_chars is not None else new), True
    lines = [*_block_note_lines(old), new]
    value = await _rewrite_block(label, lines, max_chars, max_lines) or _fit_block(lines, max_chars, max_lines)
    return value, value != old


# How the user wants to be answered can land in any of these (the extractor files it as voice or prefs,
# remember_for_me as learned), so a newer note in one must clear what it contradicts in the others.
RELATED_BLOCKS = ("voice", "prefs", "learned")


async def _drop_contradicted(label: str, value: str, newer: str) -> str:
    """``value`` without the lines that ``newer`` contradicts. The model may only remove lines: anything it returns
    that is not an existing line is ignored, and an unclear answer leaves the block as it was."""
    from app.llm import chat_complete, small_model

    lines = _block_note_lines(value)
    if not lines:
        return (value or "").strip()
    notes = "\n".join(f"{i + 1}. {line}" for i, line in enumerate(lines))
    prompt = (
        f"These notes are an assistant's '{label}' memory about one user. A newer note, which wins any conflict, "
        f'says: "{newer}". Return the notes below with every note that the newer note fully contradicts or '
        "replaces removed. Keep a note that is only partly out of date. Copy every note you keep exactly as "
        "written. Do not add the newer note or anything else. "
        "Return only the remaining notes, one per line, with no numbers or bullets, or the single word NONE if no "
        "note remains.\n\n" + notes
    )
    try:
        resp = await chat_complete(
            [{"role": "user", "content": prompt}],
            max_tokens=max(300, len(notes) // 2 + 100),
            temperature=0,
            model=small_model(),
        )
        raw = (resp.choices[0].message.content or "").strip()
    except Exception:  # noqa: BLE001 - leave the block alone
        log.info("memory block reconcile failed", exc_info=True)
        return (value or "").strip()
    if raw.upper().strip(" .") == "NONE":
        return ""
    keep = {line.casefold() for line in _block_note_lines(raw)}
    if not keep:
        return (value or "").strip()
    return "\n".join(line for line in lines if line.casefold() in keep)


async def reconcile_related(db: AsyncSession, user: User, agent, changed: str, newer: str) -> list[str]:
    """After ``newer`` was written to block ``changed``, drop what it contradicts in the related blocks."""
    if changed not in RELATED_BLOCKS or not (newer or "").strip():
        return []
    updated = []
    for label in RELATED_BLOCKS:
        if label == changed:
            continue
        try:
            prior = (await read_core(db, user, label, agent) or "").strip()
            if not prior:
                continue
            kept = await _drop_contradicted(label, prior, newer)
            if kept != prior:
                await upsert_core(db, user.id, label, kept, getattr(agent, "id", None))
                updated.append(label)
        except Exception:  # noqa: BLE001 - the new note is saved; a stale line elsewhere is the worst case
            log.info("memory: reconciling %s failed", label, exc_info=True)
    return updated


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
            about = getattr(row, "subject_name", "")
            tag = f"about {about}" if about else ("agent note" if getattr(row, "scope", SCOPE_USER) == SCOPE_AGENT else "fact")
            parts.append(f"- [{tag}] {row.text.strip()}")
    parts.extend(f"{b.label}: {b.value.strip()}" for b in other)
    if history:
        parts.append("From earlier chats")
        parts.extend(f"- {line}" for line in history)
    return "\n".join(parts).strip()


async def load_memory(db: AsyncSession, user: User, query: str, agent=None, task_key: str = "", light: bool = False) -> str:
    """Labeled persona + facts for one workspace agent. Fail-open: empty string on error. ``light`` keeps only the
    persona blocks, skipping every search, for turns that don't need them."""
    try:
        owner = await _memory_agent(db, user, agent=agent)
    except Exception as exc:  # noqa: BLE001
        log.warning("memory owner lookup failed: %s", exc)
        return ""
    try:
        blocks = await _visible_blocks(db, user, owner)
    except Exception as exc:  # noqa: BLE001
        log.warning("memory block load failed: %s", exc)
        blocks = []
    if light:
        return _assemble_memory(list(blocks), [], [])
    try:
        facts = await _ranked_facts(db, user.id, owner.id, query, limit=8)
        await _attach_subject_names(db, user.id, facts)
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
                assembled = (assembled + ("\n" if assembled else "") + "\n".join(extra)).strip()
            elif not assembled:
                assembled = letta_text
    try:
        from app.memory_engine.entities import people_context

        people = await people_context(db, user.id, query)
        if people:
            # Learned from the user's own mail and calendar (memory_engine/learn.py).
            assembled = (assembled + "\n\nPeople (from your mail and calendar)\n" + "\n".join(f"- {p}" for p in people)).strip()
    except Exception:  # noqa: BLE001 - the rest of memory still loads
        log.info("people context skipped", exc_info=True)
    try:
        from app.memory_engine.recall import recall_block

        extra_memory = await recall_block(db, user.id, owner.id, query, task_key=task_key)
        if extra_memory:
            assembled = (assembled + "\n\n" + extra_memory).strip()
    except Exception:  # noqa: BLE001 - the new stores must not blank the memory already loaded
        log.info("memory engine recall failed", exc_info=True)
    return assembled


async def _block_owner(db: AsyncSession, user: User, label: str, agent=None, agent_id=None):
    """Whose row holds this block: the user's default agent for user-level labels, else the agent itself."""
    if label in USER_LABELS:
        from app.agents.service import ensure_default_agent

        return await ensure_default_agent(db, user)
    return await _memory_agent(db, user, agent_id=agent_id, agent=agent)


async def _visible_blocks(db: AsyncSession, user: User, owner) -> list:
    """The user's own blocks (on their default agent) plus this agent's per-agent blocks."""
    from app.agents.service import ensure_default_agent

    default = await ensure_default_agent(db, user)
    rows = (
        await db.execute(
            select(MemoryBlock).where(
                MemoryBlock.user_id == user.id,
                MemoryBlock.agent_id.in_({default.id, owner.id}),
            )
        )
    ).scalars().all()
    return [
        row
        for row in rows
        if (row.label in USER_LABELS and row.agent_id == default.id)
        or (row.label not in USER_LABELS and row.agent_id == owner.id)
    ]


async def read_core(db: AsyncSession, user: User, label: str, agent=None) -> str:
    """One core block. User-level labels (voice, prefs, learned, people, work) are the same for all the user's agents."""
    owner = await _block_owner(db, user, label, agent=agent)
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


TRUST_CONFIDENCE = {"user_said": 0.9, "inferred": 0.6, "third_party": 0.5}


def _user_scope(scope: str | None) -> str:
    """Facts are about the user's world (shared by all their agents) unless explicitly an agent's own note."""
    return SCOPE_AGENT if (scope or "") == SCOPE_AGENT else SCOPE_USER


async def _active_twin(db: AsyncSession, user_id, agent_id, text: str, scope: str):
    """An active fact this agent can see with the same text (case-insensitive), if any."""
    try:
        cond = MemoryPassage.scope.in_(USER_SCOPES) if scope == SCOPE_USER else (
            (MemoryPassage.scope == SCOPE_AGENT) & (MemoryPassage.agent_id == agent_id)
        )
        return (
            await db.execute(
                select(MemoryPassage).where(
                    MemoryPassage.user_id == user_id,
                    MemoryPassage.status == STATUS_ACTIVE,
                    cond,
                    func.lower(MemoryPassage.text) == (text or "").strip().lower(),
                )
            )
        ).scalars().first()
    except Exception:  # noqa: BLE001 - saving still works without the duplicate check
        log.info("memory twin lookup skipped", exc_info=True)
        return None


async def _same_claim(db: AsyncSession, user_id, agent_id, subject_entity_id, predicate: str):
    """The active fact this new one replaces: same subject (None = the user) and same predicate."""
    if not predicate:
        return None
    try:
        subject = (
            MemoryPassage.subject_entity_id.is_(None)
            if subject_entity_id is None
            else MemoryPassage.subject_entity_id == subject_entity_id
        )
        return (
            await db.execute(
                select(MemoryPassage)
                .where(
                    MemoryPassage.user_id == user_id,
                    MemoryPassage.status == STATUS_ACTIVE,
                    or_(MemoryPassage.scope.in_(USER_SCOPES), MemoryPassage.agent_id == agent_id),
                    MemoryPassage.predicate == predicate,
                    subject,
                )
                .order_by(MemoryPassage.id.desc())
            )
        ).scalars().first()
    except Exception:  # noqa: BLE001
        log.info("memory claim lookup skipped", exc_info=True)
        return None


async def _reinforce(db: AsyncSession, row, confidence: float) -> None:
    row.reinforced = int(getattr(row, "reinforced", None) or 1) + 1
    row.confidence = min(0.99, max(float(getattr(row, "confidence", None) or 0.0), confidence) + 0.05)
    row.last_confirmed_at = _now_utc()
    await db.commit()


async def remember(
    db: AsyncSession,
    user_id: uuid.UUID,
    kind: str,
    text: str,
    agent_id=None,
    scope: str = SCOPE_USER,
    source_session_id=None,
    source_message_id=None,
    supersedes_id=None,
    *,
    subject_entity_id: int | None = None,
    predicate: str = "",
    trust: str = "user_said",
    confidence: float | None = None,
) -> MemoryPassage | None:
    """Save one fact for this user. It is shared by all of the user's agents unless scope is "agent".

    Saying a known fact again reinforces it instead of storing a copy. A fact with the same subject and predicate as
    an active one replaces it (the old one is kept as history with valid_to set)."""
    user = await db.get(User, user_id)
    if user is None:
        return None
    writer = await _memory_agent(db, user, agent_id=agent_id)
    scope = _user_scope(scope)
    if scope == SCOPE_USER:
        # A user-level fact is kept on the user's default agent, so deleting the agent that learned it (rows cascade
        # with their agent) never deletes the user's memory.
        from app.agents.service import ensure_default_agent

        owner = await ensure_default_agent(db, user)
    else:
        owner = writer
    trust = trust if trust in TRUST_CONFIDENCE else "inferred"
    confidence = float(confidence) if confidence is not None else TRUST_CONFIDENCE[trust]
    predicate = re.sub(r"[^a-z0-9_]", "", (predicate or "").lower().replace(" ", "_"))[:80]
    twin = await _active_twin(db, user_id, owner.id, text, scope)
    if twin is not None:
        await _reinforce(db, twin, confidence)
        return twin
    if not supersedes_id:
        same = await _same_claim(db, user_id, owner.id, subject_entity_id, predicate)
        supersedes_id = same.id if same is not None else None
    await _ensure_saved_letta(db, user, owner)
    vec = None
    try:
        vec = await embed_one(text)
    except Exception:  # noqa: BLE001
        pass
    now = _now_utc()
    retired = None
    if supersedes_id:
        prior = await db.get(MemoryPassage, supersedes_id)
        # Only a fact this agent can see may be replaced: a user-level fact, or this agent's own note.
        if prior is not None and _visible_fact(prior, user_id, owner.id):
            prior.status = STATUS_SUPERSEDED
            prior.valid_to = now
            retired = prior
        else:
            supersedes_id = None
    row = MemoryPassage(
        user_id=user_id,
        agent_id=owner.id,
        kind=kind,
        text=text,
        embedding=vec,
        scope=scope,
        status=STATUS_ACTIVE,
        source_session_id=source_session_id,
        source_message_id=source_message_id,
        supersedes_id=supersedes_id,
        subject_entity_id=subject_entity_id,
        predicate=predicate,
        trust=trust,
        confidence=confidence,
        reinforced=1,
        valid_from=now,
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        # Another turn saved the same fact a moment ago (unique per user): count it as a confirmation instead.
        await db.rollback()
        twin = await _active_twin(db, user_id, owner.id, text, scope)
        if twin is not None:
            await _reinforce(db, twin, confidence)
        return twin
    if retired is not None:
        await _forget_letta_text(owner, retired.text or "")
    await _write_letta_passage(owner, text)
    # Facts live only in memory_passages. They used to be copied into semantic_documents too, which recalled every
    # fact a second time under "Knowledge" and left a copy behind on forget.
    return row


async def retire_fact(db: AsyncSession, user: User, agent, fact_id: int) -> bool:
    """Mark one fact no longer true (superseded, kept as history). Only a fact this agent can see: its own or a
    user-shared one."""
    owner = await _memory_agent(db, user, agent=agent)
    row = await db.get(MemoryPassage, fact_id)
    if row is None or not _visible_fact(row, user.id, owner.id):
        return False
    row.status = STATUS_SUPERSEDED
    await db.commit()
    await _forget_letta_text(owner, row.text or "")
    return True


async def upsert_core(db: AsyncSession, user_id: uuid.UUID, label: str, value: str, agent_id) -> None:
    user = await db.get(User, user_id)
    if user is None:
        return
    owner = await _block_owner(db, user, label, agent_id=agent_id)
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
        db.add(MemoryBlock(user_id=user_id, agent_id=owner.id, label=label, value=value))
    else:
        row.value = value
    await _ensure_saved_letta(db, user, owner)
    await db.commit()
    await _write_letta_block(owner, label, value)


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
    # Memory belongs to the user: every one of their agents reads what any of them saves. `shared` is kept for old
    # callers; only the per-agent "coding" notes stay with this agent.
    owner = ctx.agent
    if tag in CORE_LABELS:
        existing = await read_core(ctx.db, ctx.user, tag, agent=owner)
        merged, changed = await merge_block(tag, existing, body)
        if changed:
            await upsert_core(ctx.db, ctx.user.id, tag, merged, getattr(owner, "id", None))
            await reconcile_related(ctx.db, ctx.user, owner, tag, body)
        return {"ok": True, "kind": "block", "label": tag, "shared": tag in USER_LABELS, "value": merged}
    await remember(ctx.db, ctx.user.id, "archival", body, getattr(owner, "id", None), scope=SCOPE_USER, trust="user_said")
    return {"ok": True, "kind": "passage", "shared": True, "text": body}


async def recall_memory(ctx, query: str = "", label: str = "") -> dict:
    missing = _need_db(ctx)
    if missing:
        return missing
    owner = await _memory_agent(ctx.db, ctx.user, agent=ctx.agent)
    tag = (label or "").strip().lower()
    blocks = await _visible_blocks(ctx.db, ctx.user, owner)
    if tag:
        blocks = [b for b in blocks if b.label == tag]
    block_rows = [{"kind": "block", "label": b.label, "text": b.value} for b in blocks if b.value]
    q = (query or "").strip()
    try:
        rows = await _ranked_facts(ctx.db, ctx.user.id, owner.id, q, limit=6) if q else await _newest_facts(
            ctx.db, ctx.user.id, owner.id, 6
        )
    except Exception:  # noqa: BLE001
        rows = []
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


async def _forget_everywhere(db: AsyncSession, user: User, owner, needle: str) -> list[str]:
    """Forget `needle` wherever this user's agents could read it: facts, the user's notes (matching lines),
    episodes, and a person or organisation with that name or email together with every claim about them.
    Only this user's rows are touched."""
    from app.models import MemoryEntity, MemoryEntityAlias, MemoryEpisode

    low = needle.lower()[:120]
    like = f"%{low}%"
    removed: list[str] = []
    entity_ids = set(
        (
            await db.execute(
                select(MemoryEntityAlias.entity_id).where(
                    MemoryEntityAlias.user_id == user.id, MemoryEntityAlias.value == low
                )
            )
        ).scalars().all()
    ) | set(
        (
            await db.execute(
                select(MemoryEntity.id).where(
                    MemoryEntity.user_id == user.id, MemoryEntity.status == "active", func.lower(MemoryEntity.name) == low
                )
            )
        ).scalars().all()
    )
    facts = (
        await db.execute(
            select(MemoryPassage).where(
                MemoryPassage.user_id == user.id,
                MemoryPassage.status != STATUS_DELETED,
                or_(MemoryPassage.scope.in_(USER_SCOPES), MemoryPassage.agent_id == owner.id),
                or_(
                    func.lower(MemoryPassage.text).like(like),
                    MemoryPassage.subject_entity_id.in_(entity_ids) if entity_ids else False,
                ),
            )
        )
    ).scalars().all()
    for row in facts:
        row.status = STATUS_DELETED
        removed.append(f"fact:{row.id}")
        await _forget_letta_text(owner, row.text or "")
    for block in await _visible_blocks(db, user, owner):
        lines = (block.value or "").splitlines()
        kept = [line for line in lines if low not in line.lower()]
        if len(kept) != len(lines):
            block.value = "\n".join(kept)
            removed.append(f"note:{block.label}")
    episodes = (
        await db.execute(
            select(MemoryEpisode).where(
                MemoryEpisode.user_id == user.id,
                MemoryEpisode.status == "active",
                or_(*[func.lower(getattr(MemoryEpisode, col)).like(like) for col in ("observation", "thoughts", "action", "result", "feedback")]),
            )
        )
    ).scalars().all()
    for ep in episodes:
        ep.status = "deleted"
        removed.append(f"episode:{ep.id}")
    if entity_ids:
        for entity in (
            await db.execute(select(MemoryEntity).where(MemoryEntity.user_id == user.id, MemoryEntity.id.in_(entity_ids)))
        ).scalars().all():
            entity.status = "deleted"
            removed.append(f"entity:{entity.id}")
        # Drop the aliases so a later mention starts a fresh record instead of reviving this one.
        for alias in (
            await db.execute(
                select(MemoryEntityAlias).where(MemoryEntityAlias.user_id == user.id, MemoryEntityAlias.entity_id.in_(entity_ids))
            )
        ).scalars().all():
            await db.delete(alias)
    return removed


async def forget_memory(ctx, memory_id: str = "", label: str = "", text: str = "") -> dict:
    missing = _need_db(ctx)
    if missing:
        return missing
    owner = await _memory_agent(ctx.db, ctx.user, agent=ctx.agent)
    removed: list[str] = []
    tag = (label or "").strip().lower()
    if tag:
        block_owner = await _block_owner(ctx.db, ctx.user, tag, agent=ctx.agent)
        row = (
            await ctx.db.execute(
                select(MemoryBlock).where(
                    MemoryBlock.user_id == ctx.user.id,
                    MemoryBlock.agent_id == block_owner.id,
                    MemoryBlock.label == tag,
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            needle = (text or "").strip()
            if needle and needle.lower() in (row.value or "").lower():
                kept = "\n".join(line for line in row.value.splitlines() if needle.lower() not in line.lower())
                row.value = kept
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
            if row is not None and _visible_fact(row, ctx.user.id, owner.id):
                row.status = STATUS_DELETED
                removed.append(f"passage:{pid}")
                await _forget_letta_text(owner, row.text or "")
    snippet = (text or "").strip()
    if snippet and not memory_id and not tag:
        removed += await _forget_everywhere(ctx.db, ctx.user, owner, snippet)
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
    """Store a short, durable preference or fact in the user's memory (the "learned" block, read by all their agents)."""
    if db is None or agent is None or getattr(agent, "user_id", None) != user.id:
        return {"saved": False, "error": "Choose one of your agents first."}
    clean_fact = " ".join((fact or "").split())[:400]
    clean_category = re.sub(r"[^a-z0-9_-]", "", (category or "preference").lower())[:30] or "preference"
    if not clean_fact:
        return {"saved": False, "error": "Add a specific preference or working detail to remember."}
    if _SECRET_LANGUAGE.search(clean_fact):
        return {"saved": False, "error": "I can’t store passwords, keys, tokens, or recovery codes in memory."}
    prior = await read_core(db, user, "learned", agent)
    # Rewritten like the other core blocks: "moved to Pune" replaces "lives in Bangalore" instead of sitting next to it.
    value, changed = await merge_block("learned", prior or "", f"{clean_category}: {clean_fact}")
    if changed:
        await upsert_core(db, user.id, "learned", value, agent.id)
        await reconcile_related(db, user, agent, "learned", clean_fact)
    return {"saved": True, "agent": agent.name, "memory": value}


async def list_agent_memory(db: AsyncSession, user: User, agent_id: uuid.UUID) -> dict:
    """Return only the selected user's selected agent memory for review."""
    from app.agents.service import get_agent

    agent = await get_agent(db, user, agent_id)
    blocks = sorted(await _visible_blocks(db, user, agent), key=lambda b: b.label)
    passages = (
        await db.execute(
            select(MemoryPassage)
            .where(
                MemoryPassage.user_id == user.id,
                MemoryPassage.status == STATUS_ACTIVE,
                or_(MemoryPassage.scope.in_(USER_SCOPES), MemoryPassage.agent_id == agent.id),
            )
            .order_by(desc(MemoryPassage.id))
            .limit(80)
        )
    ).scalars().all()
    active = [row for row in passages if _is_active(row)]
    # "shared" is what every one of the user's agents knows; "private" is this agent's own working notes.
    private = [row for row in active if getattr(row, "scope", SCOPE_AGENT) == SCOPE_AGENT]
    shared = [row for row in active if getattr(row, "scope", SCOPE_AGENT) in USER_SCOPES]
    persona = [{"label": row.label, "value": row.value} for row in blocks if row.label in PERSONA_LABELS and row.value]
    def _row(item):
        created = getattr(item, "created_at", None)
        return {
            "id": item.id,
            "kind": item.kind,
            "text": item.text,
            "scope": getattr(item, "scope", SCOPE_AGENT),
            "confidence": getattr(item, "confidence", None),
            "trust": getattr(item, "trust", None),
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
