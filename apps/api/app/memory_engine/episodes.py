"""Episodic memory. LangMem extracts the episode; Postgres stores it in full."""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field

from app.agent.memory import _SECRET_LANGUAGE

log = logging.getLogger(__name__)

_INSTRUCTIONS = (
    "Extract episodes from this personal-assistant turn. "
    "Each episode records the situation, the reasoning, the action, the outcome, and any user correction. "
    "Include failed attempts and what the user said to do differently. "
    "Do not store passwords, API keys, tokens, or recovery codes. "
    "Leave the list empty when the turn has nothing worth reusing."
)


class Episode(BaseModel):
    """One experience the agent can reuse later."""

    observation: str = Field(default="", description="The situation and relevant context")
    thoughts: str = Field(default="", description="Key considerations and reasoning")
    action: str = Field(default="", description="What was done")
    result: str = Field(default="", description="What happened")
    feedback: str = Field(default="", description="What the user corrected or confirmed")
    outcome: str = Field(default="", description="completed, failed, or corrected")


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def _secret(*parts: str) -> bool:
    return any(_SECRET_LANGUAGE.search(part or "") for part in parts)


def normalize_episodes(raw: Any) -> list[dict]:
    """LangMem returns ExtractedMemory rows. A JSON list is accepted too."""
    items = raw
    if isinstance(raw, dict):
        items = raw.get("episodes") or raw.get("memories") or []
    if not isinstance(items, list):
        items = [raw] if raw else []
    out: list[dict] = []
    for item in items:
        content = getattr(item, "content", item)
        if hasattr(content, "model_dump"):
            data = content.model_dump()
        elif isinstance(content, dict):
            data = content
        else:
            continue
        episode = {
            "observation": _clean(str(data.get("observation") or "")),
            "thoughts": _clean(str(data.get("thoughts") or "")),
            "action": _clean(str(data.get("action") or "")),
            "result": _clean(str(data.get("result") or "")),
            "feedback": _clean(str(data.get("feedback") or "")),
            "outcome": _clean(str(data.get("outcome") or "")).lower(),
        }
        if episode["outcome"] not in {"completed", "failed", "corrected", ""}:
            episode["outcome"] = ""
        if not any(episode[name] for name in ("observation", "action", "result", "feedback")):
            continue
        if _secret(*episode.values()):
            continue
        out.append(episode)
    return out


def _manager():
    from langchain_openai import ChatOpenAI
    from langmem import create_memory_manager

    from app.config import get_settings
    from app.llm import reasoning_payload, small_model

    settings = get_settings()
    # Same request shape as app.llm: the Responses API with the configured reasoning effort. Without a reasoning
    # setting the model runs at its default effort, where "temperature" is rejected (400), so every turn fell back
    # to the local extractor. No temperature is sent.
    model = ChatOpenAI(
        model=small_model(),
        api_key=settings.openai_api_key,
        use_responses_api=True,
        reasoning=reasoning_payload(),
        timeout=60,
        max_retries=1,
    )
    return create_memory_manager(
        model,
        schemas=[Episode],
        instructions=_INSTRUCTIONS,
        enable_inserts=True,
    )


async def _extract_local(user_text: str, assistant_text: str) -> list[dict]:
    from app.llm import chat_complete, small_model

    prompt = (
        _INSTRUCTIONS
        + " Return JSON only with key episodes, an array of objects with observation, thoughts, action, result, feedback, outcome.\n"
        f"User: {user_text}\nAssistant: {assistant_text}"
    )
    resp = await chat_complete(
        [{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_tokens=1200,
        temperature=0,
        model=small_model(),
    )
    raw = (resp.choices[0].message.content or "").strip()
    return normalize_episodes(json.loads(raw) if raw else {})


async def extract_episodes(user_text: str, assistant_text: str) -> list[dict]:
    """Prefer LangMem. The local extractor runs when LangMem is not installed or the call fails."""
    if _secret(user_text, assistant_text):
        return []
    try:
        manager = _manager()
        messages = [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": assistant_text},
        ]
        invoke = getattr(manager, "ainvoke", None)
        raw = await invoke({"messages": messages}) if invoke else manager.invoke({"messages": messages})
        return normalize_episodes(raw)
    except Exception as exc:  # noqa: BLE001
        log.info("langmem episode extract failed: %s", type(exc).__name__)
    try:
        return await _extract_local(user_text, assistant_text)
    except Exception as exc:  # noqa: BLE001
        log.info("episode extract failed: %s", type(exc).__name__)
        return []


async def _scope(db, session_id) -> tuple[str, Any]:
    if db is None or not session_id:
        return "agent", None
    from app.models import ChatSession

    try:
        row = await db.get(ChatSession, session_id)
    except Exception:  # noqa: BLE001
        return "agent", None
    group_id = getattr(row, "group_id", None) if row is not None else None
    if group_id:
        return "group", group_id
    return "agent", None


async def save_episode(
    db,
    user_id,
    agent_id,
    episode: dict,
    *,
    session_id=None,
    scope: str = "agent",
    group_id=None,
) -> None:
    if db is None or _secret(*episode.values()):
        return
    from app.llm import embed_one
    from app.models import MemoryEpisode

    blob = "\n".join(episode.get(name) or "" for name in ("observation", "thoughts", "action", "result", "feedback"))
    vec = None
    try:
        vec = await embed_one(blob) if blob.strip() else None
    except Exception:  # noqa: BLE001
        vec = None
    db.add(
        MemoryEpisode(
            user_id=user_id,
            agent_id=agent_id,
            group_id=group_id,
            scope=scope,
            observation=episode.get("observation") or "",
            thoughts=episode.get("thoughts") or "",
            action=episode.get("action") or "",
            result=episode.get("result") or "",
            feedback=episode.get("feedback") or "",
            outcome=episode.get("outcome") or "",
            embedding=vec,
            source_session_id=session_id,
        )
    )
    await db.commit()


async def record_turn(db, user, agent, user_text: str, assistant_text: str, *, session_id=None) -> int:
    episodes = await extract_episodes(user_text, assistant_text)
    if not episodes:
        return 0
    scope, group_id = await _scope(db, session_id)
    agent_id = getattr(agent, "id", None)
    saved = 0
    for episode in episodes:
        await save_episode(
            db,
            user.id,
            agent_id,
            episode,
            session_id=session_id,
            scope=scope,
            group_id=group_id,
        )
        saved += 1
    return saved


async def save_outcome(state: dict, *, ok: bool) -> None:
    """Write a completed or failed task without another model call."""
    from app.db import SessionLocal

    notes = " ".join(str(item.get("text") or "") for item in (state.get("notes") or []) if isinstance(item, dict))
    episode = {
        "observation": str(state.get("skill_name") or state.get("task_key") or ""),
        "thoughts": "",
        "action": notes,
        "result": str(state.get("result") or ""),
        "feedback": "",
        "outcome": "completed" if ok else "failed",
    }
    if not any(episode.values()):
        return
    user_id = state.get("user_id")
    if not user_id:
        return
    async with SessionLocal() as db:
        import uuid

        uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
        agent_id = state.get("agent_id")
        aid = None
        if agent_id:
            try:
                aid = uuid.UUID(str(agent_id))
            except (TypeError, ValueError):
                aid = None
        session_id = state.get("session_id")
        sid = None
        if session_id:
            try:
                sid = uuid.UUID(str(session_id))
            except (TypeError, ValueError):
                sid = None
        await save_episode(db, uid, aid, episode, session_id=sid)
