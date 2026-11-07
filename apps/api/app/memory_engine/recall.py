"""Assemble the current task, matching episodes, pinned procedure, and semantic documents."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

EPISODE_LIMIT = 6
MAX_EPISODE_DISTANCE = 0.65  # cosine distance; further than this an episode is unrelated and only adds noise
_CANDIDATES = 20
_RRF_K = 60  # the usual reciprocal rank fusion constant


def _section(title: str, lines: list[str]) -> str:
    body = [line for line in lines if line]
    if not body:
        return ""
    return title + "\n" + "\n".join(body)


def fuse_ranks(*rankings: list, limit: int = EPISODE_LIMIT) -> list:
    """Reciprocal rank fusion: an item near the top of either list ranks well, and one in both ranks best."""
    scores: dict = {}
    for ranking in rankings:
        for position, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + 1.0 / (_RRF_K + position)
    return sorted(scores, key=lambda item: -scores[item])[:limit]


def _episode_text(row) -> str:
    return " ".join(part for part in (row.observation, row.thoughts, row.action, row.result, row.feedback) if part)


def _episode_line(row) -> str:
    outcome = f" ({row.outcome})" if row.outcome else ""
    return f"- {_episode_text(row)}{outcome}"


async def _episodes(db, user_id, agent_id, query: str) -> list[str]:
    from sqlalchemy import or_, select

    from app.models import MemoryEpisode

    # Episodes are what happened in this user's conversations: every one of the user's agents can recall them.
    # Only this user's rows (user_id) are ever read.
    visible = select(MemoryEpisode).where(
        MemoryEpisode.user_id == user_id,
        MemoryEpisode.status == "active",
    )
    tokens = [tok for tok in (query or "").lower().split() if len(tok) > 2][:8]
    if not tokens:
        rows = (await db.execute(visible.order_by(MemoryEpisode.id.desc()).limit(3))).scalars().all()
        return [_episode_line(row) for row in rows]

    columns = (
        MemoryEpisode.observation,
        MemoryEpisode.thoughts,
        MemoryEpisode.action,
        MemoryEpisode.result,
        MemoryEpisode.feedback,
    )
    matches = [column.icontains(tok, autoescape=True) for tok in tokens for column in columns]
    keyword_rows = (
        await db.execute(visible.where(or_(*matches)).order_by(MemoryEpisode.id.desc()).limit(_CANDIDATES * 3))
    ).scalars().all()

    def words_found(row) -> int:
        blob = _episode_text(row).lower()
        return sum(tok in blob for tok in tokens)

    keyword_rows = sorted(keyword_rows, key=lambda row: (-words_found(row), -row.id))[:_CANDIDATES]

    vector_rows: list = []
    try:
        from app.llm import embed_one

        vec = await embed_one(query)
        distance = MemoryEpisode.embedding.cosine_distance(vec)
        async with db.begin_nested():  # a failed search must not undo the rest of the turn's work
            vector_rows = list(
                (
                    await db.execute(
                        visible.where(MemoryEpisode.embedding.is_not(None), distance <= MAX_EPISODE_DISTANCE)
                        .order_by(distance)
                        .limit(_CANDIDATES)
                    )
                ).scalars().all()
            )
    except Exception as exc:  # noqa: BLE001 - keyword matches still work
        log.info("episode vector search skipped: %s", type(exc).__name__)

    by_id = {row.id: row for row in [*keyword_rows, *vector_rows]}
    best = fuse_ranks([row.id for row in vector_rows], [row.id for row in keyword_rows])
    return [_episode_line(by_id[item]) for item in best]


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
