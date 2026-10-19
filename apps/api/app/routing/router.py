from __future__ import annotations

import json
import time
from collections import defaultdict

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.llm import chat_complete
from app.models import Connector, RouteLog, ServerCard
from app.routing.select import (
    RouteDecision,
    ToolHit,
    add_neighbors,
    cap_tools,
    core_tools,
    fill_prereqs,
    groups_not_loaded,
    search_tools,
    usage_bonus,
    user_connectors,
)
from app.routing.describe import description_score, mentioned_connectors
from app.routing.split import explicit_slugs, first_party_tag, is_followup, split_subtasks


def _by_slug(connectors: list[Connector]) -> dict[str, Connector]:
    out = {}
    for c in connectors:
        out[c.slug] = c
        if c.alias:
            out[c.alias.lower().replace(" ", "-")] = c
        out[c.name.lower().replace(" ", "-")] = c
    return out


def _for_tag(connectors: list[Connector], tag: str) -> list[Connector]:
    defaults = [c for c in connectors if tag in (c.default_for or [])]
    tagged = [c for c in connectors if tag in (c.tags or [])]
    return defaults or tagged


async def _assemble(db, connectors: list[Connector], query: str, extra_hits: list[ToolHit] | None = None) -> list[ToolHit]:
    subtasks = split_subtasks(query)
    hits: list[ToolHit] = []
    for part in subtasks:
        hits.extend(await search_tools(db, connectors, part, limit=8))
    if extra_hits:
        hits.extend(extra_hits)
    hits.extend(await core_tools(db, connectors))
    qlow = query.lower()
    extra_names: set[tuple[str, str]] = set()
    if any(k in qlow for k in ("attach", "pdf", "download", "preview", "open")):
        for c in connectors:
            extra_names |= {
                (c.slug, n)
                for n in ("get_message", "list_attachments", "download_attachment", "summarize_attachments", "read_attachment_text")
            }
    if any(k in qlow for k in ("invite", "schedule", "meeting", "calendar", "event", "book")):
        for c in connectors:
            extra_names |= {(c.slug, n) for n in ("create_event", "list_events", "find_free_slots")}
    if any(
        k in qlow
        for k in (
            "briefing",
            "waiting",
            "promised",
            "commitment",
            "today",
            "invoice",
            "receipt",
            "automat",
            "voice",
            "style",
            "every day",
            "every evening",
            "every week",
            "remind me",
            "scheduled",
            "in 10 minute",
            "in 5 minute",
        )
    ):
        for c in connectors:
            extra_names |= {
                (c.slug, n)
                for n in (
                    "today_briefing",
                    "ask_inbox",
                    "scan_commitments",
                    "list_commitments",
                    "scan_admin",
                    "schedule_task",
                    "list_scheduled_tasks",
                    "update_scheduled_task",
                    "create_automation",
                    "list_automations",
                    "learn_voice",
                )
            }
    if extra_names:
        from app.routing.select import load_named

        hits.extend(await load_named(db, connectors, extra_names))
    hits = await fill_prereqs(db, connectors, hits)
    hits = await add_neighbors(db, hits, connectors)
    return cap_tools(hits)


async def route_query(
    db: AsyncSession,
    user_id,
    session_id,
    request_id: str,
    query: str,
    working: dict,
) -> RouteDecision:
    started = time.monotonic()
    connectors = await user_connectors(db, user_id)
    if not connectors:
        return RouteDecision(path="none", connector_ids=[], clarify=[{"reason": "no_connectors"}])

    slugs = explicit_slugs(query)
    by_slug = _by_slug(connectors)
    confirm_tools = working.get("confirm_tools") or []
    if confirm_tools:
        wanted = [by_slug[t["slug"]] for t in confirm_tools if t.get("slug") in by_slug]
        if not wanted:
            last_ids = working.get("last_connector_ids") or []
            wanted = [c for c in connectors if str(c.id) in last_ids]
        if wanted:
            from app.routing.select import load_named

            extra = await load_named(
                db,
                wanted,
                {(t["slug"], t["tool"]) for t in confirm_tools if t.get("slug") and t.get("tool")},
            )
            tools = await _assemble(db, wanted, query, extra_hits=extra)
            decision = RouteDecision(path="confirm", connector_ids=[str(c.id) for c in wanted], tools=tools)
            await _log(db, request_id, user_id, session_id, decision, started)
            return decision
    explicit = [by_slug[s] for s in slugs if s in by_slug]
    if not explicit:
        explicit = mentioned_connectors(query, connectors)
    if explicit:
        tools = await _assemble(db, explicit, query)
        decision = RouteDecision(path="explicit", connector_ids=[str(c.id) for c in explicit], tools=tools)
        await _log(db, request_id, user_id, session_id, decision, started)
        return decision

    desc_ranked = sorted(((c, description_score(query, c)) for c in connectors), key=lambda item: item[1], reverse=True)
    if desc_ranked and desc_ranked[0][1] >= 0.4:
        top_c, top_d = desc_ranked[0]
        second_d = desc_ranked[1][1] if len(desc_ranked) > 1 else 0.0
        if top_d - second_d >= 0.15 or top_d >= 0.7:
            chosen = [top_c]
            if len(split_subtasks(query)) > 1:
                chosen = [c for c, score in desc_ranked[:3] if score >= 0.25] or [top_c]
            tools = await _assemble(db, chosen, query)
            decision = RouteDecision(
                path="description",
                connector_ids=[str(c.id) for c in chosen],
                tools=tools,
                candidates=[{"connector_id": str(c.id), "score": sc, "name": c.alias or c.name} for c, sc in desc_ranked[:5]],
            )
            await _log(db, request_id, user_id, session_id, decision, started)
            return decision

    last_ids = working.get("last_connector_ids") or []
    if is_followup(query) and last_ids:
        sticky = [c for c in connectors if str(c.id) in last_ids]
        if sticky:
            tools = await _assemble(db, sticky, query)
            decision = RouteDecision(path="followup", connector_ids=[str(c.id) for c in sticky], tools=tools)
            await _log(db, request_id, user_id, session_id, decision, started)
            return decision

    fp = first_party_tag(query)
    if fp:
        intent, tag = fp
        chosen = _for_tag(connectors, tag)
        if chosen:
            tools = await _assemble(db, chosen, query)
            decision = RouteDecision(path="rule", connector_ids=[str(c.id) for c in chosen], tools=tools)
            await _log(db, request_id, user_id, session_id, decision, started)
            return decision

    hits = []
    for part in split_subtasks(query):
        hits.extend(await search_tools(db, connectors, part, 15))
    scored: dict[str, float] = defaultdict(float)
    by_id = {str(c.id): c for c in connectors}
    for h in hits:
        scored[h.connector_id] = max(scored[h.connector_id], h.score)
    for cid, score in list(scored.items()):
        conn = by_id.get(cid)
        if conn is None:
            continue
        scored[cid] = score + await usage_bonus(db, user_id, conn.id) + description_score(query, conn)
        if any(t in (conn.default_for or []) for t in (conn.tags or [])):
            scored[cid] += 0.04
    for conn in connectors:
        cid = str(conn.id)
        extra = description_score(query, conn)
        if extra and cid not in scored:
            scored[cid] = extra

    ranked = sorted(scored.items(), key=lambda kv: kv[1], reverse=True)
    candidates = [{"connector_id": cid, "score": sc, "name": by_id[cid].name} for cid, sc in ranked[:5]]
    if not ranked:
        decision = RouteDecision(path="empty", connector_ids=[], candidates=candidates)
        await _log(db, request_id, user_id, session_id, decision, started)
        return decision

    top_id, top_score = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = get_settings().route_margin
    if top_score - second >= margin or len(split_subtasks(query)) > 1:
        if len(split_subtasks(query)) > 1:
            chosen = [by_id[cid] for cid, score in ranked[:3] if cid in by_id]
        else:
            chosen = [by_id[top_id]]
        tools = await _assemble(db, chosen, query, extra_hits=hits)
        decision = RouteDecision(path="retrieval", connector_ids=[str(c.id) for c in chosen], tools=tools, candidates=candidates)
        await _log(db, request_id, user_id, session_id, decision, started)
        return decision

    top_conns = [by_id[cid] for cid, _ in ranked[:4]]
    cards = []
    for c in top_conns:
        card = await db.get(ServerCard, c.server_key) if c.server_key else None
        user_desc = (getattr(c, "description", "") or "").strip()
        summary = user_desc or (card.card if card else c.name)
        cards.append({"id": str(c.id), "name": c.alias or c.name, "slug": c.slug, "card": summary[:400]})
    tool_lines = [f"{h.slug}.{h.tool_name}: {h.description[:80]}" for h in hits[:12]]
    prompt = (
        "Choose which connectors and tools to use. Return JSON "
        '{"confidence":0-1,"steps":[{"connector_id":"...","tool":"..."}],"ambiguous":false}. '
        f"Query: {query}\nConnectors: {json.dumps(cards)}\nCandidate tools:\n" + "\n".join(tool_lines)
    )
    try:
        resp = await chat_complete(
            [{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=400,
        )
        plan = json.loads(resp.choices[0].message.content or "{}")
    except Exception:  # noqa: BLE001
        plan = {"confidence": 0.3, "ambiguous": True, "steps": []}

    if plan.get("ambiguous") or float(plan.get("confidence") or 0) < 0.45:
        last_ids = working.get("last_connector_ids") or []
        sticky = [c for c in connectors if str(c.id) in last_ids]
        if sticky:
            tools = await _assemble(db, sticky, query)
            decision = RouteDecision(path="followup", connector_ids=[str(c.id) for c in sticky], tools=tools)
            await _log(db, request_id, user_id, session_id, decision, started)
            return decision
        decision = RouteDecision(
            path="clarify",
            connector_ids=[],
            candidates=candidates,
            clarify=[{"id": c["id"], "name": c["name"], "slug": c.get("slug")} for c in cards],
        )
        await _log(db, request_id, user_id, session_id, decision, started)
        return decision

    ids = []
    for step in plan.get("steps") or []:
        if step.get("connector_id") in by_id:
            ids.append(step["connector_id"])
    ids = list(dict.fromkeys(ids)) or [top_id]
    chosen = [by_id[i] for i in ids]
    tools = await _assemble(db, chosen, query, extra_hits=hits)
    hints = []
    for c in chosen:
        card = await db.get(ServerCard, c.server_key) if c.server_key else None
        hints.extend(groups_not_loaded(card, [h for h in tools if h.connector_id == str(c.id)]))
    decision = RouteDecision(
        path="planner",
        connector_ids=ids,
        tools=tools,
        groups_hint=hints,
        steps=plan.get("steps") or [],
        candidates=candidates,
    )
    await _log(db, request_id, user_id, session_id, decision, started)
    return decision


async def _log(db, request_id, user_id, session_id, decision: RouteDecision, started: float) -> None:
    db.add(
        RouteLog(
            request_id=request_id,
            user_id=user_id,
            session_id=session_id,
            path=decision.path,
            candidates=decision.candidates,
            chosen={"connectors": decision.connector_ids, "tools": [f"{t.slug}.{t.tool_name}" for t in decision.tools]},
            latency_ms=(time.monotonic() - started) * 1000,
        )
    )
    await db.commit()
