from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.llm import embed_one
from app.models import Connector, ServerCard, ToolEdge, ToolIndex, ToolUsage


@dataclass
class ToolHit:
    connector_id: str
    connector_name: str
    slug: str
    server_key: str
    tool_name: str
    description: str
    group: str
    schema: dict
    read_only: bool
    destructive: bool
    requires: list[str]
    produces: list[str]
    score: float


@dataclass
class RouteDecision:
    path: str
    connector_ids: list[str]
    tools: list[ToolHit] = field(default_factory=list)
    groups_hint: list[str] = field(default_factory=list)
    clarify: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)


def openai_tool(hit: ToolHit) -> dict:
    return {
        "type": "function",
        "function": {
            "name": f"{hit.slug}__{hit.tool_name}",
            "description": f"[{hit.connector_name}] {hit.description}",
            "parameters": hit.schema or {"type": "object", "properties": {}},
        },
    }


def parse_tool_name(qualified: str) -> tuple[str, str]:
    if "__" not in qualified:
        return "", qualified
    slug, name = qualified.split("__", 1)
    return slug, name


async def user_connectors(db: AsyncSession, user_id) -> list[Connector]:
    return list(
        (
            await db.execute(
                select(Connector).where(
                    Connector.user_id == user_id,
                    Connector.enabled.is_(True),
                    Connector.health.in_(("ok", "pending")),
                )
            )
        )
        .scalars()
        .all()
    )


async def search_tools(db: AsyncSession, connectors: list[Connector], query: str, limit: int = 15) -> list[ToolHit]:
    keys = [c.server_key for c in connectors if c.server_key]
    if not keys:
        return []
    by_key = defaultdict(list)
    for c in connectors:
        if c.server_key:
            by_key[c.server_key].append(c)

    vector = await embed_one(query)
    vec_lit = "[" + ",".join(f"{x:.7f}" for x in vector) + "]"
    sql = text(
        """
        SELECT tool_name, description, "group", input_schema, read_only, destructive,
               requires, produces, server_key,
               (1 - (embedding <=> CAST(:vec AS vector))) AS vscore,
               ts_rank(search_tsv, plainto_tsquery('simple', :q)) AS tscore
        FROM tool_index
        WHERE server_key = ANY(:keys)
          AND (embedding IS NOT NULL OR search_tsv @@ plainto_tsquery('simple', :q))
        ORDER BY (COALESCE(1 - (embedding <=> CAST(:vec AS vector)), 0) * 0.7
                  + COALESCE(ts_rank(search_tsv, plainto_tsquery('simple', :q)), 0) * 0.3) DESC
        LIMIT :limit
        """
    )
    rows = (await db.execute(sql, {"vec": vec_lit, "q": query, "keys": keys, "limit": limit * 3})).all()
    hits: list[ToolHit] = []
    for row in rows:
        for conn in by_key.get(row.server_key, []):
            hits.append(
                ToolHit(
                    connector_id=str(conn.id),
                    connector_name=conn.alias or conn.name,
                    slug=conn.slug,
                    server_key=row.server_key,
                    tool_name=row.tool_name,
                    description=row.description,
                    group=row.group,
                    schema=row.input_schema or {},
                    read_only=row.read_only,
                    destructive=row.destructive,
                    requires=list(row.requires or []),
                    produces=list(row.produces or []),
                    score=float((row.vscore or 0) * 0.7 + (row.tscore or 0) * 0.3),
                )
            )
    hits.sort(key=lambda h: h.score, reverse=True)
    per_conn = max(3, limit // max(1, len(connectors)))
    picked: list[ToolHit] = []
    used: set[tuple[str, str]] = set()
    counts: dict[str, int] = defaultdict(int)
    for hit in hits:
        key = (hit.slug, hit.tool_name)
        if key in used or counts[hit.slug] >= per_conn:
            continue
        used.add(key)
        counts[hit.slug] += 1
        picked.append(hit)
        if len(picked) >= limit:
            return picked
    for hit in hits:
        key = (hit.slug, hit.tool_name)
        if key in used:
            continue
        used.add(key)
        picked.append(hit)
        if len(picked) >= limit:
            break
    return picked


async def usage_bonus(db: AsyncSession, user_id, connector_id) -> float:
    rows = (
        await db.execute(
            select(ToolUsage.success, ToolUsage.failure).where(
                ToolUsage.user_id == user_id, ToolUsage.connector_id == connector_id
            )
        )
    ).all()
    ok = sum(r[0] for r in rows)
    bad = sum(r[1] for r in rows)
    return min(0.15, ok / max(1, ok + bad) * 0.15) if ok else 0.0


async def load_named(db: AsyncSession, connectors: list[Connector], names: set[tuple[str, str]]) -> list[ToolHit]:
    if not names:
        return []
    by_slug = {c.slug: c for c in connectors}
    hits = []
    for slug, tool in names:
        conn = by_slug.get(slug)
        if conn is None or not conn.server_key:
            continue
        row = (
            await db.execute(
                select(ToolIndex).where(ToolIndex.server_key == conn.server_key, ToolIndex.tool_name == tool)
            )
        ).scalar_one_or_none()
        if row is None:
            continue
        hits.append(
            ToolHit(
                connector_id=str(conn.id),
                connector_name=conn.alias or conn.name,
                slug=conn.slug,
                server_key=row.server_key,
                tool_name=row.tool_name,
                description=row.description,
                group=row.group,
                schema=row.input_schema or {},
                read_only=row.read_only,
                destructive=row.destructive,
                requires=list(row.requires or []),
                produces=list(row.produces or []),
                score=1.0,
            )
        )
    return hits


async def fill_prereqs(db: AsyncSession, connectors: list[Connector], chosen: list[ToolHit]) -> list[ToolHit]:
    have = {p for h in chosen for p in h.produces}
    need = {r for h in chosen for r in h.requires} - have
    if not need:
        return chosen
    extra = []
    for conn in connectors:
        if not conn.server_key:
            continue
        rows = (
            await db.execute(select(ToolIndex).where(ToolIndex.server_key == conn.server_key, ToolIndex.read_only.is_(True)))
        ).scalars().all()
        for row in rows:
            if any(p in need for p in (row.produces or [])):
                extra.append(
                    ToolHit(
                        connector_id=str(conn.id),
                        connector_name=conn.alias or conn.name,
                        slug=conn.slug,
                        server_key=row.server_key,
                        tool_name=row.tool_name,
                        description=row.description,
                        group=row.group,
                        schema=row.input_schema or {},
                        read_only=row.read_only,
                        destructive=row.destructive,
                        requires=list(row.requires or []),
                        produces=list(row.produces or []),
                        score=0.2,
                    )
                )
                need -= set(row.produces or [])
                if not need:
                    break
    seen = {(h.slug, h.tool_name) for h in chosen}
    for h in extra:
        if (h.slug, h.tool_name) not in seen:
            chosen.append(h)
            seen.add((h.slug, h.tool_name))
    return chosen


async def add_neighbors(db: AsyncSession, chosen: list[ToolHit], connectors: list[Connector]) -> list[ToolHit]:
    if not chosen:
        return chosen
    keys = {h.server_key for h in chosen}
    names = {h.tool_name for h in chosen}
    edges = (
        await db.execute(select(ToolEdge).where(ToolEdge.server_key.in_(keys), ToolEdge.from_tool.in_(names)))
    ).scalars().all()
    edges.sort(key=lambda e: e.count, reverse=True)
    wanted = {(e.server_key, e.to_tool) for e in edges[:3]}
    pairs = set()
    by_key = {c.server_key: c for c in connectors if c.server_key}
    for server_key, tool in wanted:
        conn = by_key.get(server_key)
        if conn:
            pairs.add((conn.slug, tool))
    extra = await load_named(db, connectors, pairs)
    seen = {(h.slug, h.tool_name) for h in chosen}
    for h in extra:
        if (h.slug, h.tool_name) not in seen:
            chosen.append(h)
    return chosen


async def core_tools(db: AsyncSession, connectors: list[Connector]) -> list[ToolHit]:
    hits = []
    for conn in connectors:
        if not conn.server_key:
            continue
        card = await db.get(ServerCard, conn.server_key)
        names = (card.core_tools if card else [])[:8]
        loaded = await load_named(db, [conn], {(conn.slug, n) for n in names})
        hits.extend(loaded)
    return hits


def cap_tools(hits: list[ToolHit]) -> list[ToolHit]:
    limit = get_settings().max_tool_schemas
    seen = set()
    out = []
    for h in hits:
        key = (h.slug, h.tool_name)
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
        if len(out) >= limit:
            break
    return out


def groups_not_loaded(card: ServerCard | None, loaded: list[ToolHit]) -> list[str]:
    if card is None:
        return []
    used = {h.group for h in loaded}
    hints = []
    for group, info in (card.groups or {}).items():
        if group not in used:
            hints.append(f"{group}: {info.get('label', group)}")
    return hints[:8]
