"""Semantic memory: full documents in Postgres, searched with pgvector.

Graphiti is called only when NEO4J_URI and NEO4J_PASSWORD are set. A missing graph
database leaves the document store in place and does not fail the turn.
"""

from __future__ import annotations

import asyncio
import logging
import os

log = logging.getLogger(__name__)

_CHUNK = 2000
_TASKS: set[asyncio.Task] = set()
_graph = None
_graph_failed = False


def graph_configured() -> bool:
    from app.config import get_settings

    settings = get_settings()
    return bool((settings.neo4j_uri or "").strip() and (settings.neo4j_password or "").strip())


def chunk_text(body: str, size: int = _CHUNK) -> list[str]:
    """Index pages over a document. The document row keeps the whole text."""
    text = body or ""
    if not text:
        return []
    if len(text) <= size:
        return [text]
    pages = []
    start = 0
    while start < len(text):
        pages.append(text[start : start + size])
        start += size
    return pages


async def _embed(text: str):
    from app.llm import embed_one

    try:
        return await embed_one(text)
    except Exception:  # noqa: BLE001
        return None


async def remember_fact_text(db, user_id, text: str, *, agent_id=None, scope: str = "agent") -> None:
    """Store one standing fact as a semantic document and, when configured, a graph episode."""
    if db is None or not (text or "").strip():
        return
    await store_document(
        db,
        user_id,
        body=text,
        title=text.strip().splitlines()[0][:200],
        source="fact",
        scope=scope,
        agent_id=agent_id,
    )


async def store_document(
    db,
    user_id,
    *,
    body: str,
    title: str = "",
    source: str = "document",
    scope: str = "agent",
    agent_id=None,
    group_id=None,
) -> int | None:
    from app.models import SemanticChunk, SemanticDocument

    text = body or ""
    if not text.strip():
        return None
    doc = SemanticDocument(
        user_id=user_id,
        agent_id=agent_id,
        group_id=group_id,
        scope=scope or "agent",
        source=source or "document",
        title=title or "",
        body=text,
    )
    db.add(doc)
    await db.flush()
    for index, page in enumerate(chunk_text(text)):
        db.add(
            SemanticChunk(
                document_id=doc.id,
                user_id=user_id,
                position=index,
                text=page,
                embedding=await _embed(page),
            )
        )
    await db.commit()
    schedule_graph(text, title or source, str(user_id))
    return doc.id


def schedule_graph(text: str, name: str, group: str) -> None:
    if not graph_configured() or not (text or "").strip():
        return
    task = asyncio.create_task(_push_graph(text, name, group))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def _client():
    global _graph, _graph_failed
    if _graph is not None or _graph_failed or not graph_configured():
        return _graph
    from app.config import get_settings

    settings = get_settings()
    os.environ["GRAPHITI_TELEMETRY_ENABLED"] = "true" if settings.graphiti_telemetry_enabled else "false"
    try:
        from graphiti_core import Graphiti
    except Exception as exc:  # noqa: BLE001
        _graph_failed = True
        log.info("graphiti is not available: %s", type(exc).__name__)
        return None
    try:
        client = Graphiti(settings.neo4j_uri, settings.neo4j_user or "neo4j", settings.neo4j_password)
        await client.build_indices_and_constraints()
    except Exception as exc:  # noqa: BLE001
        _graph_failed = True
        log.info("graphiti connection skipped: %s", type(exc).__name__)
        return None
    _graph = client
    return client


async def _push_graph(text: str, name: str, group: str) -> None:
    from datetime import datetime, timezone

    try:
        from graphiti_core.nodes import EpisodeType
    except Exception:  # noqa: BLE001
        EpisodeType = None
    try:
        client = await asyncio.wait_for(_client(), timeout=15)
        if client is None:
            return
        kwargs = {
            "name": (name or "memory")[:200],
            "episode_body": text,
            "source_description": f"lumio:{group}",
            "reference_time": datetime.now(timezone.utc),
        }
        if EpisodeType is not None:
            kwargs["source"] = EpisodeType.text
        await asyncio.wait_for(client.add_episode(**kwargs), timeout=30)
    except Exception as exc:  # noqa: BLE001
        log.info("graphiti episode skipped: %s", type(exc).__name__)


async def search_documents(db, user_id, agent_id, query: str, *, limit: int = 4) -> list[dict]:
    """Return the full body of the documents whose index pages match the query."""
    from sqlalchemy import select, text

    from app.models import SemanticDocument

    if db is None or not (query or "").strip():
        return []
    ids: list[int] = []
    vec = await _embed(query)
    if vec:
        vec_lit = "[" + ",".join(f"{float(x):.7f}" for x in vec) + "]"
        try:
            rows = (
                await db.execute(
                    text(
                        """
                        SELECT document_id
                        FROM semantic_chunks
                        WHERE user_id = :uid AND embedding IS NOT NULL
                          AND document_id IN (
                            SELECT id FROM semantic_documents
                            WHERE user_id = :uid AND status = 'active'
                              AND (
                                agent_id = :aid
                                OR scope = 'user_shared'
                                OR (
                                  scope = 'group'
                                  AND group_id IN (
                                    SELECT group_id FROM agent_group_members WHERE agent_id = :aid
                                  )
                                )
                              )
                          )
                        ORDER BY embedding <=> CAST(:vec AS vector)
                        LIMIT :lim
                        """
                    ),
                    {"uid": user_id, "aid": agent_id, "vec": vec_lit, "lim": limit},
                )
            ).all()
            ids = [row[0] for row in rows]
        except Exception as exc:  # noqa: BLE001
            log.info("semantic vector search skipped: %s", type(exc).__name__)
    stmt = select(SemanticDocument).where(
        SemanticDocument.user_id == user_id,
        SemanticDocument.status == "active",
    )
    if ids:
        stmt = stmt.where(SemanticDocument.id.in_(ids))
    else:
        needle = query.strip()[:80]
        stmt = stmt.where(SemanticDocument.body.ilike(f"%{needle}%")).limit(limit)
    try:
        docs = (await db.execute(stmt)).scalars().all()
    except Exception as exc:  # noqa: BLE001
        log.info("semantic document search skipped: %s", type(exc).__name__)
        return []
    order = {doc_id: index for index, doc_id in enumerate(ids)}
    docs.sort(key=lambda doc: order.get(doc.id, 99))
    groups: set = set()
    try:
        from app.models import AgentGroupMember

        groups = set(
            (
                await db.execute(select(AgentGroupMember.group_id).where(AgentGroupMember.agent_id == agent_id))
            ).scalars().all()
        )
    except Exception:  # noqa: BLE001
        groups = set()
    visible = []
    for doc in docs:
        shared = doc.scope == "user_shared"
        in_group = doc.scope == "group" and doc.group_id in groups
        if doc.agent_id == agent_id or shared or in_group:
            visible.append({"source": doc.source, "title": doc.title or "", "body": doc.body or ""})
    return visible[:limit]
