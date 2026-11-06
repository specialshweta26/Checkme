"""Working memory for the task that is running now.

Redis keeps the hot copy. Postgres keeps the checkpoint, because the Key Value instance
evicts keys and a deploy restarts the API process.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

log = logging.getLogger(__name__)

_TTL_S = 36 * 3600


def _key(user_id, task_key: str) -> str:
    return f"lumio:work:{user_id}:{task_key}"


def _merge(prior: dict | None, **parts: Any) -> dict:
    base = {
        "context": {},
        "browser": {},
        "variables": {},
        "progress": {},
        "agent_id": None,
        "session_id": None,
    }
    if prior:
        for name in ("context", "browser", "variables", "progress"):
            if isinstance(prior.get(name), dict):
                base[name] = dict(prior[name])
        base["agent_id"] = prior.get("agent_id")
        base["session_id"] = prior.get("session_id")
    for name in ("context", "browser", "variables", "progress"):
        incoming = parts.get(name)
        if isinstance(incoming, dict) and incoming:
            base[name].update(incoming)
    if parts.get("agent_id"):
        base["agent_id"] = str(parts["agent_id"])
    if parts.get("session_id"):
        base["session_id"] = str(parts["session_id"])
    return base


def _loads(raw) -> dict | None:
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


async def _redis_get(client, key: str) -> dict | None:
    if client is None:
        return None
    try:
        return _loads(await client.get(key))
    except Exception as exc:  # noqa: BLE001
        log.info("working memory redis read skipped: %s", type(exc).__name__)
        return None


async def _redis_set(client, key: str, payload: dict) -> None:
    if client is None:
        return
    try:
        await client.set(key, json.dumps(payload).encode(), ex=_TTL_S)
    except Exception as exc:  # noqa: BLE001
        log.info("working memory redis write skipped: %s", type(exc).__name__)


def _uuid(value) -> uuid.UUID | None:
    if not value:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


async def _persist(session_factory, user_id, task_key: str, payload: dict) -> None:
    if session_factory is None:
        from app.db import SessionLocal

        session_factory = SessionLocal
    from sqlalchemy import select

    from app.models import WorkflowCheckpoint

    uid = _uuid(user_id)
    if uid is None or not task_key:
        return
    async with session_factory() as db:
        row = (
            await db.execute(
                select(WorkflowCheckpoint).where(
                    WorkflowCheckpoint.user_id == uid,
                    WorkflowCheckpoint.task_key == task_key,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = WorkflowCheckpoint(user_id=uid, task_key=task_key)
            db.add(row)
        row.agent_id = _uuid(payload.get("agent_id"))
        row.session_id = _uuid(payload.get("session_id"))
        row.context = payload.get("context") or {}
        row.browser = payload.get("browser") or {}
        row.variables = payload.get("variables") or {}
        row.progress = payload.get("progress") or {}
        await db.commit()


async def _read_db(session_factory, user_id, task_key: str) -> dict | None:
    if session_factory is None:
        from app.db import SessionLocal

        session_factory = SessionLocal
    from sqlalchemy import select

    from app.models import WorkflowCheckpoint

    uid = _uuid(user_id)
    if uid is None:
        return None
    async with session_factory() as db:
        row = (
            await db.execute(
                select(WorkflowCheckpoint).where(
                    WorkflowCheckpoint.user_id == uid,
                    WorkflowCheckpoint.task_key == task_key,
                )
            )
        ).scalar_one_or_none()
    if row is None:
        return None
    return {
        "context": row.context or {},
        "browser": row.browser or {},
        "variables": row.variables or {},
        "progress": row.progress or {},
        "agent_id": str(row.agent_id) if row.agent_id else None,
        "session_id": str(row.session_id) if row.session_id else None,
    }


async def save_working(
    user_id,
    task_key: str,
    *,
    agent_id=None,
    session_id=None,
    context: dict | None = None,
    browser: dict | None = None,
    variables: dict | None = None,
    progress: dict | None = None,
    redis=None,
    session_factory=None,
) -> dict:
    """Merge one task's working memory and write Redis, then Postgres."""
    if not task_key:
        return {}
    if redis is None and session_factory is None:
        from app.cache import get_redis

        redis = get_redis()
    key = _key(user_id, task_key)
    prior = await _redis_get(redis, key)
    if prior is None:
        try:
            prior = await _read_db(session_factory, user_id, task_key)
        except Exception as exc:  # noqa: BLE001
            log.info("working memory checkpoint read skipped: %s", type(exc).__name__)
            prior = None
    payload = _merge(
        prior,
        agent_id=agent_id,
        session_id=session_id,
        context=context,
        browser=browser,
        variables=variables,
        progress=progress,
    )
    await _redis_set(redis, key, payload)
    try:
        await _persist(session_factory, user_id, task_key, payload)
    except Exception as exc:  # noqa: BLE001
        log.info("working memory checkpoint skipped: %s", type(exc).__name__)
    return payload


async def load_working(user_id, task_key: str, *, redis=None, session_factory=None) -> dict:
    if not task_key:
        return {}
    if redis is None and session_factory is None:
        from app.cache import get_redis

        redis = get_redis()
    key = _key(user_id, task_key)
    hit = await _redis_get(redis, key)
    if hit:
        return hit
    try:
        stored = await _read_db(session_factory, user_id, task_key)
    except Exception as exc:  # noqa: BLE001
        log.info("working memory checkpoint read skipped: %s", type(exc).__name__)
        return {}
    if stored and redis is not None:
        await _redis_set(redis, key, stored)
    return stored or {}


def from_task_state(state: dict) -> dict:
    """The checkpoint fields a skill task already tracks."""
    return {
        "agent_id": state.get("agent_id"),
        "session_id": state.get("session_id"),
        "context": {"result": state.get("result") or ""},
        "variables": {"artifacts": list(state.get("artifacts") or [])},
        "progress": {
            "skill_id": state.get("skill_id"),
            "skill_name": state.get("skill_name") or "",
            "skill_version": state.get("skill_version") or "",
            "status": state.get("status") or "",
            "step_index": int(state.get("step_index") or 0),
            "step_count": int(state.get("step_count") or 0),
            "notes": list(state.get("notes") or []),
        },
    }


async def note_browser(ctx, url: str = "", detail: dict | None = None) -> None:
    """Record the live browser position on the current task. No task means no write."""
    key = getattr(ctx, "task_key", "") or ""
    user = getattr(ctx, "user", None)
    if not key or user is None:
        return
    browser: dict = {}
    if url:
        browser["url"] = url
    if isinstance(detail, dict):
        page = detail.get("page") if isinstance(detail.get("page"), dict) else {}
        for source in (detail, page):
            for name in ("url", "title", "tab"):
                if source.get(name):
                    browser[name] = source.get(name)
    if not browser:
        return
    try:
        await save_working(
            user.id,
            key,
            agent_id=getattr(getattr(ctx, "agent", None), "id", None),
            session_id=getattr(ctx, "session_id", None),
            browser=browser,
        )
    except Exception as exc:  # noqa: BLE001
        log.info("browser working memory skipped: %s", type(exc).__name__)


async def save_from_task(state: dict) -> None:
    parts = from_task_state(state)
    await save_working(
        state.get("user_id"),
        str(state.get("task_key") or ""),
        agent_id=parts["agent_id"],
        session_id=parts["session_id"],
        context=parts["context"],
        variables=parts["variables"],
        progress=parts["progress"],
    )
