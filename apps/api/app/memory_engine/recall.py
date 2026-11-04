"""Assemble the current task, matching episodes, pinned procedure, and semantic documents."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _section(title: str, lines: list[str]) -> str:
    body = [line for line in lines if line]
    if not body:
        return ""
    return title + "\n" + "\n".join(body)


async def _episodes(db, user_id, agent_id, query: str) -> list[str]:
    from sqlalchemy import or_, select

    from app.models import AgentGroupMember, MemoryEpisode

    member_groups = select(AgentGroupMember.group_id).where(AgentGroupMember.agent_id == agent_id)
    rows = (
        await db.execute(
            select(MemoryEpisode)
            .where(
                MemoryEpisode.user_id == user_id,
                MemoryEpisode.status == "active",
                or_(
                    MemoryEpisode.agent_id == agent_id,
                    MemoryEpisode.scope == "user_shared",
                    (MemoryEpisode.scope == "group") & MemoryEpisode.group_id.in_(member_groups),
                ),
            )
            .order_by(MemoryEpisode.id.desc())
            .limit(40)
        )
    ).scalars().all()
    tokens = [tok for tok in (query or "").lower().split() if len(tok) > 2][:8]
    ranked = []
    for row in rows:
        blob = " ".join(
            part for part in (row.observation, row.thoughts, row.action, row.result, row.feedback) if part
        )
        if tokens and not any(tok in blob.lower() for tok in tokens):
            continue
        if not tokens and len(ranked) >= 3:
            break
        outcome = f" ({row.outcome})" if row.outcome else ""
        ranked.append(f"- {blob}{outcome}")
        if len(ranked) >= 6:
            break
    return ranked


async def _procedure(db, progress: dict) -> str:
    from sqlalchemy import select

    from app.models import AgentSkillVersion

    skill_id = progress.get("skill_id")
    revision = str(progress.get("skill_version") or "")
    if not skill_id or not revision.isdigit():
        return ""
    import uuid

    try:
        sid = uuid.UUID(str(skill_id))
    except (TypeError, ValueError):
        return ""
    row = (
        await db.execute(
            select(AgentSkillVersion).where(
                AgentSkillVersion.skill_id == sid,
                AgentSkillVersion.revision == int(revision),
            )
        )
    ).scalar_one_or_none()
    if row is None or not (row.instruction or row.steps):
        return ""
    lines = [f"{row.name} revision {row.revision}"]
    if row.instruction:
        lines.append(row.instruction)
    return "\n".join(lines)


async def recall_block(db, user_id, agent_id, query: str, *, task_key: str = "") -> str:
    if db is None:
        return ""
    parts: list[str] = []
    if task_key:
        try:
            from app.memory_engine.working import load_working

            state = await load_working(user_id, task_key)
        except Exception as exc:  # noqa: BLE001
            log.info("working memory load skipped: %s", type(exc).__name__)
            state = {}
        if state:
            progress = state.get("progress") or {}
            browser = state.get("browser") or {}
            variables = state.get("variables") or {}
            context = state.get("context") or {}
            lines = []
            if progress:
                lines.append(
                    "Progress: "
                    + ", ".join(f"{key}={value}" for key, value in progress.items() if key != "notes" and value not in ("", None, []))
                )
                for note in progress.get("notes") or []:
                    if isinstance(note, dict) and note.get("text"):
                        lines.append(f"- {note['text']}")
            if browser:
                lines.append("Browser: " + ", ".join(f"{key}={value}" for key, value in browser.items() if value))
            if variables:
                lines.append("Variables: " + str(variables))
            if context.get("result"):
                lines.append("Result so far: " + str(context["result"]))
            block = _section("Current task", lines)
            if block:
                parts.append(block)
            try:
                procedure = await _procedure(db, progress)
            except Exception as exc:  # noqa: BLE001
                log.info("pinned procedure skipped: %s", type(exc).__name__)
                procedure = ""
            if procedure:
                parts.append("Pinned procedure\n" + procedure)
    try:
        episodes = await _episodes(db, user_id, agent_id, query)
    except Exception as exc:  # noqa: BLE001
        log.info("episode recall skipped: %s", type(exc).__name__)
        episodes = []
    block = _section("Episodes", episodes)
    if block:
        parts.append(block)
    try:
        from app.memory_engine.semantic import search_documents

        docs = await search_documents(db, user_id, agent_id, query)
    except Exception as exc:  # noqa: BLE001
        log.info("semantic recall skipped: %s", type(exc).__name__)
        docs = []
    knowledge = []
    for doc in docs:
        title = doc.get("title") or doc.get("source") or "document"
        knowledge.append(f"- [{doc.get('source') or 'document'}] {title}\n{doc.get('body') or ''}")
    block = _section("Knowledge", knowledge)
    if block:
        parts.append(block)
    return "\n\n".join(parts)
