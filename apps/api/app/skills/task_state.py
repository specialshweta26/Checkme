"""Per-task skill state: which skill a task follows, how far it got, notes and artifacts.

A *task* is one background job (``job:<id>``) or one agent's thread in a chat (``chat:<session>:<agent>``), so
concurrent tasks never share state. Every change is committed in its own session (a checkpoint), so a task picks up
where it stopped after a restart or a job retry. A handover to another agent copies the state into a child row; the
child's result is written back onto the originating task's state when it finishes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

CHAT_RESUME_HOURS = 6  # a chat task with no routed skill resumes its active skill within this window
CONFIRM_TIMEOUT = 12
MAX_NOTES = 40
MAX_ARTIFACTS = 30
OPEN = {"active", "handed_off"}

_NUMBERED = re.compile(r"^\s*(?:step\s*)?\d{1,2}[.)]\s+\S", re.I | re.M)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


def task_key_for(session_id=None, agent_id=None, job_id=None) -> str:
    if job_id:
        return f"job:{job_id}"
    return f"chat:{session_id or 'none'}:{agent_id or 'default'}"


def count_steps(skill: dict) -> int:
    """Numbered lines in the instruction, else recorded steps."""
    numbered = len(_NUMBERED.findall(skill.get("instruction") or ""))
    return numbered or int(skill.get("step_count") or len(skill.get("steps") or []))


# ---------------------------------------------------------------- persistence (swappable in tests)


class DbStore:
    """Each call commits on its own session: one call is one checkpoint."""

    async def find_open(self, user_id, task_key: str) -> dict | None:
        from sqlalchemy import select

        from app.db import SessionLocal
        from app.models import TaskSkillState

        async with SessionLocal() as db:
            row = (
                await db.execute(
                    select(TaskSkillState)
                    .where(
                        TaskSkillState.user_id == uuid.UUID(str(user_id)),
                        TaskSkillState.task_key == task_key,
                        TaskSkillState.status.in_(list(OPEN)),
                    )
                    .order_by(TaskSkillState.updated_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            return _row_dict(row) if row is not None else None

    async def get(self, state_id) -> dict | None:
        from app.db import SessionLocal
        from app.models import TaskSkillState

        async with SessionLocal() as db:
            row = await db.get(TaskSkillState, uuid.UUID(str(state_id)))
            return _row_dict(row) if row is not None else None

    async def create(self, data: dict) -> dict:
        from app.db import SessionLocal
        from app.models import TaskSkillState

        async with SessionLocal() as db:
            row = TaskSkillState(**{k: _uuid_or(v) if k.endswith("_id") else v for k, v in data.items() if k != "id"})
            db.add(row)
            await db.commit()
            await db.refresh(row)
            return _row_dict(row)

    async def update(self, state_id, **fields) -> dict | None:
        from app.db import SessionLocal
        from app.models import TaskSkillState

        async with SessionLocal() as db:
            row = await db.get(TaskSkillState, uuid.UUID(str(state_id)))
            if row is None:
                return None
            for key, value in fields.items():
                setattr(row, key, value)
            row.updated_at = _now()
            await db.commit()
            await db.refresh(row)
            return _row_dict(row)

    async def list_for(self, user_id, session_id=None, job_id=None, limit: int = 20) -> list[dict]:
        from sqlalchemy import or_, select

        from app.db import SessionLocal
        from app.models import TaskSkillState

        async with SessionLocal() as db:
            stmt = select(TaskSkillState).where(TaskSkillState.user_id == uuid.UUID(str(user_id)))
            if session_id:
                stmt = stmt.where(TaskSkillState.session_id == uuid.UUID(str(session_id)))
            if job_id:
                jid = uuid.UUID(str(job_id))
                stmt = stmt.where(or_(TaskSkillState.job_id == jid, TaskSkillState.task_key == f"job:{jid}"))
            rows = (await db.execute(stmt.order_by(TaskSkillState.updated_at.desc()).limit(limit))).scalars().all()
            return [_row_dict(r) for r in rows]


def _uuid_or(value):
    if value in (None, ""):
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _row_dict(row) -> dict:
    out = {}
    for key in (
        "id", "user_id", "agent_id", "task_key", "session_id", "job_id", "parent_id", "skill_id", "skill_name",
        "skill_version", "status", "step_index", "step_count", "notes", "artifacts", "result", "created_at", "updated_at",
    ):
        value = getattr(row, key, None)
        if isinstance(value, uuid.UUID):
            value = str(value)
        out[key] = value
    out["notes"] = list(out.get("notes") or [])
    out["artifacts"] = list(out.get("artifacts") or [])
    return out


STORE: Any = DbStore()


def serialize(state: dict | None) -> dict | None:
    if not state:
        return None
    return {
        **{k: state.get(k) for k in ("id", "task_key", "parent_id", "skill_id", "skill_name", "skill_version", "status", "step_index", "step_count", "result")},
        "agent_id": state.get("agent_id"),
        "notes": list(state.get("notes") or [])[-10:],
        "artifacts": list(state.get("artifacts") or [])[-10:],
        "updated_at": _iso(state.get("updated_at")),
    }


def step_label(state: dict) -> str:
    total = int(state.get("step_count") or 0)
    done = int(state.get("step_index") or 0)
    if total:
        return f"step {min(done + 1, total)}/{total}" if done < total else f"all {total} steps done"
    return f"step {done + 1}"


# ---------------------------------------------------------------- choosing the skill


async def confirm_skills(query: str, candidates: list[dict], llm=None) -> list[dict]:
    """LLM check that routed skills really fit this request. Keeps the candidates if the check fails."""
    if not candidates:
        return []
    menu = [{"id": str(c.get("id")), "name": c.get("name"), "use_when": (c.get("instruction") or "")[:300]} for c in candidates]
    messages = [
        {"role": "system", "content": "You decide which saved skills (step-by-step workflows) apply to a user's request."},
        {
            "role": "user",
            "content": "Request: " + (query or "")[:1500] + "\n\nCandidate skills: " + json.dumps(menu)
            + '\n\nReply in JSON: {"use": [ids of skills that the request is asking to perform, best first; [] if none]}. '
            "A question about the agent itself, or a different task that only shares a word, uses none.",
        },
    ]
    try:
        if llm is None:
            from app.config import get_settings

            if not get_settings().openai_api_key:
                return [{**c, "match": {**(c.get("match") or {}), "confirmed": None}} for c in candidates]
            from app.llm import chat_complete as llm
        resp = await asyncio.wait_for(llm(messages, response_format={"type": "json_object"}, max_tokens=200), CONFIRM_TIMEOUT)
        text = resp.choices[0].message.content if hasattr(resp, "choices") else str(resp)
        data = json.loads(text or "{}")
        keep = [str(x) for x in (data.get("use") or [])]
    except Exception as exc:  # noqa: BLE001 - routing alone still works
        log.info("skills: confirm step unavailable (%s); keeping routed skills", type(exc).__name__)
        return [{**c, "match": {**(c.get("match") or {}), "confirmed": None}} for c in candidates]
    by_id = {str(c.get("id")): c for c in candidates}
    return [{**by_id[i], "match": {**(by_id[i].get("match") or {}), "confirmed": True}} for i in keep if i in by_id]


async def begin(
    user, task_key: str, agent_id=None, skill: dict | None = None, session_id=None, job_id=None, meta_question: bool = False
) -> tuple[dict | None, bool]:
    """The state this run follows, and whether it resumed an earlier one.

    Jobs keep their state for their whole life (retries and restarts resume). In chat a newly routed skill replaces
    an older one; with nothing routed the active skill resumes if it is recent. Meta questions never resume a task.
    """
    try:
        current = await STORE.find_open(user.id, task_key)
    except Exception as exc:  # noqa: BLE001 - never block a turn on task state
        log.info("task state: lookup failed (%s)", type(exc).__name__)
        return None, False
    is_job = task_key.startswith(("job:", "peer:"))  # whole-life tasks: retries and handovers keep their state
    if current is not None:
        same = skill is not None and str(skill.get("id")) == str(current.get("skill_id"))
        fresh = _age_ok(current.get("updated_at"))
        if is_job:  # one task, one skill: a retry or a different routing result keeps the state
            return current, True
        if not is_job and same:
            return current, True
        if not is_job and skill is None:
            return (current, True) if (fresh and not meta_question and current.get("status") == "active") else (None, False)
        await STORE.update(current["id"], status="dropped")
    if skill is None:
        return None, False
    state = await STORE.create(
        {
            "user_id": str(user.id),
            "agent_id": str(agent_id) if agent_id else None,
            "task_key": task_key,
            "session_id": str(session_id) if session_id else None,
            "job_id": str(job_id) if job_id else None,
            "skill_id": str(skill.get("id")) if skill.get("id") else None,
            "skill_name": str(skill.get("name") or "")[:160],
            "skill_version": str(skill.get("updated_at") or skill.get("created_at") or "")[:40],
            "status": "active",
            "step_index": 0,
            "step_count": count_steps(skill),
            "notes": [],
            "artifacts": [],
            "result": "",
        }
    )
    return state, False


def _age_ok(value) -> bool:
    if not isinstance(value, datetime):
        try:
            value = datetime.fromisoformat(str(value))
        except ValueError:
            return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return _now() - value <= timedelta(hours=CHAT_RESUME_HOURS)


async def checkpoint(
    state_id, step_index: int | None = None, note: str = "", artifact: dict | None = None, status: str = "", by: str = ""
) -> dict | None:
    state = await STORE.get(state_id)
    if state is None:
        return None
    fields: dict[str, Any] = {}
    if step_index is not None:
        total = int(state.get("step_count") or 0)
        step = max(0, int(step_index))
        fields["step_index"] = min(step, total) if total else step
    if note.strip():
        fields["notes"] = (list(state.get("notes") or []) + [{"at": _iso(_now()), "text": note.strip()[:1000], "by": by}])[-MAX_NOTES:]
    if artifact and (artifact.get("value") or artifact.get("label")):
        item = {"at": _iso(_now()), "label": str(artifact.get("label") or "")[:120], "value": str(artifact.get("value") or "")[:2000], "by": by}
        fields["artifacts"] = (list(state.get("artifacts") or []) + [item])[-MAX_ARTIFACTS:]
    if status:
        fields["status"] = status
    if not fields:
        return state
    return await STORE.update(state_id, **fields)


# ---------------------------------------------------------------- handover


async def handover(state: dict, to_agent_id, task_key: str, by: str = "", to_name: str = "", job_id=None) -> dict:
    """Pass the task to another agent: a child state starting at the same skill and step."""
    child = await STORE.create(
        {
            "user_id": state["user_id"],
            "agent_id": str(to_agent_id) if to_agent_id else None,
            "task_key": task_key,
            "session_id": state.get("session_id"),
            "job_id": str(job_id) if job_id else None,
            "parent_id": state["id"],
            "skill_id": state.get("skill_id"),
            "skill_name": state.get("skill_name") or "",
            "skill_version": state.get("skill_version") or "",
            "status": "active",
            "step_index": int(state.get("step_index") or 0),
            "step_count": int(state.get("step_count") or 0),
            "notes": list(state.get("notes") or []) + [{"at": _iso(_now()), "text": f"Handed over by {by or 'a teammate'}.", "by": by}],
            "artifacts": list(state.get("artifacts") or []),
            "result": "",
        }
    )
    await checkpoint(state["id"], note=f"Handed {step_label(state)} to {to_name or 'a teammate'}.", status="handed_off", by=by)
    return child


async def finish(state_id, result: str = "", ok: bool = True, by: str = "") -> dict | None:
    """Close a task state; a handed-over child writes its result back to the originating task."""
    state = await STORE.get(state_id)
    if state is None or state.get("status") not in OPEN:
        return state
    done = await STORE.update(state_id, status="done" if ok else "failed", result=(result or "")[:4000])
    parent_id = state.get("parent_id")
    if parent_id:
        parent = await STORE.get(parent_id)
        if parent is not None:
            fields: dict[str, Any] = {
                "notes": (list(parent.get("notes") or []) + [{"at": _iso(_now()), "text": f"Result from {by or 'teammate'}: {(result or '')[:900]}", "by": by}])[-MAX_NOTES:],
                "step_index": max(int(parent.get("step_index") or 0), int(state.get("step_index") or 0)),
                "artifacts": _merge_artifacts(parent.get("artifacts"), state.get("artifacts")),
            }
            if parent.get("status") == "handed_off":
                fields["status"] = "active"
            await STORE.update(parent_id, **fields)
    return done


def _merge_artifacts(mine, theirs) -> list:
    out = list(mine or [])
    seen = {(a.get("label"), a.get("value")) for a in out}
    for item in theirs or []:
        if (item.get("label"), item.get("value")) not in seen:
            out.append(item)
    return out[-MAX_ARTIFACTS:]


async def finish_job(job) -> None:
    """Called when a background job reaches a terminal status."""
    try:
        state = await STORE.find_open(job.user_id, task_key_for(job_id=job.id))
        if state is None:
            return
        agent_name = (getattr(job, "meta", None) or {}).get("agent_name") or ""
        await finish(state["id"], job.result if job.status == "done" else (job.error or job.status), ok=job.status == "done", by=agent_name)
    except Exception as exc:  # noqa: BLE001
        log.info("task state: closing job state failed (%s)", type(exc).__name__)


# ---------------------------------------------------------------- prompt


def prompt_block(state: dict | None, resumed: bool = False) -> str:
    if not state or not state.get("skill_name"):
        return ""
    lines = [
        "\n## Task state",
        f"This task follows your skill \"{state['skill_name']}\" (id {state.get('skill_id')}, version {state.get('skill_version') or 'n/a'}). "
        f"Progress: {int(state.get('step_index') or 0)} of {int(state.get('step_count') or 0) or '?'} steps done; next is {step_label(state)}.",
    ]
    if resumed:
        lines.append("You are resuming this task: do not redo finished steps; continue from the next one.")
    if state.get("parent_id"):
        lines.append("A teammate handed this task to you with this state. Do your part, then reply with the result; it returns to them.")
    notes = list(state.get("notes") or [])[-6:]
    if notes:
        lines.append("Progress notes: " + " | ".join(str(n.get("text") or "") for n in notes))
    arts = list(state.get("artifacts") or [])[-6:]
    if arts:
        lines.append("Artifacts so far: " + "; ".join(f"{a.get('label')}: {str(a.get('value'))[:200]}" for a in arts))
    lines.append(
        "After finishing each step, call skill_progress(step=<steps done>, note=..., artifact_label/artifact_value for "
        "anything produced). When handing the task to a teammate, the state goes with it."
    )
    return "\n".join(lines) + "\n"
