from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.sanitize import clean_description, destructive_name, infer_group, infer_ids
from app.llm import chat_complete, embed_texts
from app.models import ServerCard, ToolIndex
from connector_core import ToolSpec

log = logging.getLogger(__name__)

CORE_HINTS = ("search", "list", "get", "find", "whoami", "status")
MAIL_CORE_TOOLS = (
    "search_messages",
    "get_message",
    "send_message",
    "reply",
    "forward",
    "list_attachments",
    "summarize_attachments",
    "list_messages",
    "create_draft",
    "search_events",
)


def server_key_for(kind: str, provider: str, url: str | None) -> str:
    if kind == "builtin":
        return f"builtin:{provider}"
    host = (url or "").rstrip("/").lower()
    return "mcp:" + hashlib.sha256(host.encode()).hexdigest()[:32]


def spec_from_mcp(tool) -> ToolSpec:
    schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {"type": "object", "properties": {}}
    desc = clean_description(getattr(tool, "description", "") or "")
    ann = getattr(tool, "annotations", None)
    read_only = bool(getattr(ann, "readOnlyHint", True) if ann is not None else True)
    destructive = bool(getattr(ann, "destructiveHint", False) if ann is not None else False) or destructive_name(tool.name)
    requires, produces = infer_ids(tool.name, schema)
    group = infer_group(tool.name)
    tags = [group]
    return ToolSpec(
        name=tool.name,
        description=desc,
        input_schema=schema,
        group=group,
        read_only=read_only and not destructive,
        destructive=destructive,
        requires=tuple(requires),
        produces=tuple(produces),
        tags=tuple(tags),
    )


def _search_text(spec: ToolSpec) -> str:
    props = " ".join((spec.input_schema.get("properties") or {}).keys())
    return f"{spec.name} {spec.description} {spec.group} {props} {' '.join(spec.tags)}"


def _cluster(specs: list[ToolSpec]) -> dict:
    groups: dict[str, list[str]] = defaultdict(list)
    for s in specs:
        groups[s.group].append(s.name)
    return {g: {"label": g.replace("_", " ").replace(".", " "), "tools": names} for g, names in groups.items()}


def _core(specs: list[ToolSpec]) -> list[str]:
    names = {s.name for s in specs}
    if {"search_messages", "get_message", "send_message"} <= names:
        return [n for n in MAIL_CORE_TOOLS if n in names]
    ranked = []
    for s in specs:
        score = sum(1 for h in CORE_HINTS if h in s.name.lower())
        if s.read_only:
            score += 1
        ranked.append((score, s.name))
    ranked.sort(reverse=True)
    return [n for _, n in ranked[:6]]


async def upsert_tool_index(db: AsyncSession, server_key: str, title: str, specs: list[ToolSpec]) -> None:
    existing = (
        await db.execute(select(ToolIndex.tool_name, ToolIndex.search_text).where(ToolIndex.server_key == server_key))
    ).all()
    existing_map = {name: text for name, text in existing}
    wanted = {s.name for s in specs}
    stale = set(existing_map) - wanted
    if stale:
        await db.execute(delete(ToolIndex).where(ToolIndex.server_key == server_key, ToolIndex.tool_name.in_(stale)))

    to_embed: list[ToolSpec] = []
    for spec in specs:
        text = _search_text(spec)
        if existing_map.get(spec.name) != text:
            to_embed.append(spec)

    vectors: dict[str, list[float]] = {}
    if to_embed:
        try:
            vecs = await embed_texts([_search_text(s) for s in to_embed])
            vectors = {s.name: v for s, v in zip(to_embed, vecs, strict=True)}
        except Exception as exc:  # noqa: BLE001
            log.warning("embedding failed for %s: %s", server_key, exc)

    by_name = {s.name: s for s in specs}
    rows = (await db.execute(select(ToolIndex).where(ToolIndex.server_key == server_key))).scalars().all()
    row_map = {r.tool_name: r for r in rows}
    for spec in specs:
        text = _search_text(spec)
        row = row_map.get(spec.name)
        if row is None:
            row = ToolIndex(server_key=server_key, tool_name=spec.name)
            db.add(row)
        row.description = spec.description
        row.group = spec.group
        row.input_schema = spec.input_schema
        row.read_only = spec.read_only
        row.destructive = spec.destructive
        row.requires = list(spec.requires)
        row.produces = list(spec.produces)
        row.tags = list(spec.tags)
        row.search_text = text
        if spec.name in vectors:
            row.embedding = vectors[spec.name]

    card = await db.get(ServerCard, server_key)
    groups = _cluster(specs)
    core = _core(specs)
    if card is None:
        card = ServerCard(server_key=server_key, title=title)
        db.add(card)
    card.title = title
    card.groups = groups
    card.core_tools = core
    card.tool_count = len(specs)
    card.tags = sorted({t for s in specs for t in s.tags}) or ["tools"]
    if not card.card:
        card.card = await _capability_card(title, specs)
    await db.commit()
    _ = by_name


async def _capability_card(title: str, specs: list[ToolSpec]) -> str:
    sample = ", ".join(s.name for s in specs[:20])
    prompt = (
        f"Write a 2-3 sentence capability card for an MCP/tool server named {title}. "
        f"Tools include: {sample}. Also list 3-6 short tags such as mail, calendar, docs, tickets, chat, files, crm. "
        "Return JSON {card, tags}."
    )
    try:
        resp = await chat_complete(
            [{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=250,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        tags = data.get("tags") or []
        return (data.get("card") or title)[:600] + ("\nTags: " + ", ".join(tags) if tags else "")
    except Exception as exc:  # noqa: BLE001
        log.warning("capability card failed: %s", exc)
        groups = sorted({s.group for s in specs})
        return f"{title} exposes {len(specs)} tools in {', '.join(groups)}."


async def mark_synced(db: AsyncSession, connector) -> None:
    connector.last_synced_at = datetime.now(timezone.utc)
    connector.health = "ok"
    connector.health_detail = ""
    await db.commit()
