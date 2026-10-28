"""Save a workflow, or teach one by demonstration on the computer, optionally narrated out loud."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.collab import TEACH_MAX_SECONDS
from app.config import get_settings
from app.models import AgentSkill, User, WorkspaceAgent
from app.skills import narration, recorder

log = logging.getLogger(__name__)

_TEACH: dict[str, dict[str, Any]] = {}
# Extra time after the limit for the browser to upload narration before the server saves on its own.
AUTO_SAVE_GRACE = 45.0
MAX_STEPS = 400
MAX_AUDIO_CLIPS = 12


class SkillError(ValueError):
    pass


def _key(user_id) -> str:
    return str(user_id)


def teach_limit() -> int:
    """Seconds one teaching session may last (TEACH_MAX_SECONDS env, default ten minutes)."""
    try:
        value = int(get_settings().teach_max_seconds or TEACH_MAX_SECONDS)
    except (TypeError, ValueError):
        value = TEACH_MAX_SECONDS
    return max(60, min(value, 3600))


def _elapsed(row: dict) -> float:
    return max(0.0, time.monotonic() - float(row.get("started_at") or 0))


def teaching_session(user_id) -> dict[str, Any] | None:
    row = _TEACH.get(_key(user_id))
    if not row:
        return None
    if _elapsed(row) >= float(row.get("max_seconds") or teach_limit()):
        return None
    return row


def record_computer_step(user_id, tool_name: str, args: dict | None = None) -> None:
    """An agent computer tool ran while the user is teaching."""
    row = teaching_session(user_id)
    if not row:
        return
    steps = row.setdefault("steps", [])
    if len(steps) >= MAX_STEPS:
        return
    steps.append({"t": round(_elapsed(row), 2), "kind": "tool", "tool": tool_name, "args": args or {}, "at": time.time()})


def record_user_input(user_id, kind: str, **fields: Any) -> None:
    """Input the user sent through the rail (typing bar, quick keys, click-to-control on a frame)."""
    row = teaching_session(user_id)
    if not row:
        return
    steps = row.setdefault("steps", [])
    if len(steps) >= MAX_STEPS:
        return
    clean = {k: (v[:2000] if isinstance(v, str) else v) for k, v in fields.items() if v not in (None, "")}
    steps.append({"t": round(_elapsed(row), 2), "kind": kind, "source": "rail", **clean})


def serialize_skill(row: AgentSkill) -> dict:
    return {
        "id": str(row.id),
        "agent_id": str(row.agent_id) if row.agent_id else None,
        "name": row.name,
        "kind": row.kind,
        "steps": row.steps or [],
        "instruction": row.instruction or "",
        "duration_ms": row.duration_ms or 0,
        "created_at": row.created_at.isoformat() if row.created_at else "",
    }


async def list_skills(db: AsyncSession, user: User, agent_id: uuid.UUID | None = None) -> list[dict]:
    stmt = select(AgentSkill).where(AgentSkill.user_id == user.id).order_by(AgentSkill.created_at.desc())
    if agent_id:
        stmt = stmt.where(or_(AgentSkill.agent_id == agent_id, AgentSkill.agent_id.is_(None)))
    rows = (await db.execute(stmt)).scalars().all()
    return [serialize_skill(r) for r in rows]


async def get_skill(
    db: AsyncSession, user: User, skill_id: str = "", name: str = "", agent_id: uuid.UUID | None = None
) -> AgentSkill | None:
    if skill_id:
        try:
            uid = uuid.UUID(str(skill_id))
        except (TypeError, ValueError):
            uid = None
        if uid is not None:
            row = await db.get(AgentSkill, uid)
            if row is not None and row.user_id == user.id:
                return row
    title = " ".join((name or "").split())
    if not title:
        return None
    stmt = select(AgentSkill).where(AgentSkill.user_id == user.id, AgentSkill.name.ilike(title))
    if agent_id:
        stmt = stmt.where(or_(AgentSkill.agent_id == agent_id, AgentSkill.agent_id.is_(None)))
    stmt = stmt.order_by(AgentSkill.created_at.desc())
    return (await db.execute(stmt)).scalars().first()


async def update_skill(
    db: AsyncSession,
    user: User,
    *,
    skill_id: str = "",
    name: str = "",
    instruction: str | None = None,
    new_name: str = "",
    agent_id: uuid.UUID | None = None,
) -> AgentSkill:
    row = await get_skill(db, user, skill_id, name, agent_id)
    if row is None:
        raise SkillError("Unknown skill.")
    if new_name:
        title = " ".join(new_name.split())
        if not title:
            raise SkillError("Skill name is required.")
        row.name = title[:160]
    if instruction is not None:
        row.instruction = instruction[:8000]
    await db.commit()
    await db.refresh(row)
    return row


async def delete_skill(
    db: AsyncSession, user: User, skill_id: str = "", name: str = "", agent_id: uuid.UUID | None = None
) -> dict:
    row = await get_skill(db, user, skill_id, name, agent_id)
    if row is None:
        raise SkillError("Unknown skill.")
    payload = serialize_skill(row)
    await db.delete(row)
    await db.commit()
    return {"ok": True, "deleted": payload}


def match_skills(skills: list[dict], query: str, limit: int = 3) -> list[dict]:
    q = (query or "").lower()
    if not q or not skills:
        return []
    words = [w for w in re.findall(r"[a-z0-9]{3,}", q)]
    scored: list[tuple[int, dict]] = []
    for skill in skills:
        hay = f"{skill.get('name') or ''} {skill.get('instruction') or ''}".lower()
        score = 0
        if (skill.get("name") or "").lower() in q or q in hay:
            score += 5
        score += sum(2 if w in (skill.get("name") or "").lower() else 1 for w in words if w in hay)
        if score:
            scored.append((score, skill))
    scored.sort(key=lambda item: (-item[0], item[1].get("name") or ""))
    return [row for _, row in scored[: max(1, min(int(limit or 3), 6))]]


def skills_prompt(skills: list[dict] | None) -> str:
    rows = [s for s in (skills or []) if s.get("name")]
    if not rows:
        return ""
    lines = ["Saved skills that match this request (follow them unless the user says otherwise):"]
    for skill in rows:
        bit = (skill.get("instruction") or "").strip() or "(taught steps on the computer screen)"
        lines.append(f"- {skill['name']}: {bit[:400]}")
    return "\n".join(lines) + "\n"


async def save_skill(
    db: AsyncSession,
    user: User,
    *,
    name: str,
    agent_id: uuid.UUID | None = None,
    kind: str = "saved",
    steps: list | None = None,
    instruction: str = "",
    duration_ms: int = 0,
) -> AgentSkill:
    title = " ".join((name or "").split())
    if not title:
        raise SkillError("Skill name is required.")
    if agent_id:
        agent = await db.get(WorkspaceAgent, agent_id)
        if agent is None or agent.user_id != user.id:
            raise SkillError("Unknown agent")
    row = AgentSkill(
        user_id=user.id,
        agent_id=agent_id,
        name=title[:160],
        kind="taught" if kind == "taught" else "saved",
        steps=list(steps or []),
        instruction=(instruction or "")[:8000],
        duration_ms=max(0, int(duration_ms or 0)),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


def start_teaching(user: User, agent_id: uuid.UUID | None = None) -> dict:
    """Record a demonstration on the computer for up to ``teach_limit()`` seconds.

    The server records what happens on the screen; the browser may add microphone narration clips
    (``add_narration``), which are transcribed and lined up with the actions when the skill is saved.
    """
    key = _key(user.id)
    old = _TEACH.pop(key, None)
    if old and old.get("auto_save"):
        old["auto_save"].cancel()
    limit = teach_limit()
    row: dict[str, Any] = {
        "agent_id": str(agent_id) if agent_id else None,
        "started_at": time.monotonic(),
        "started_wall": time.time(),
        "max_seconds": limit,
        "steps": [],
        "audio": [],
    }
    _TEACH[key] = row
    recorder.start(key, row["started_at"], limit)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        row["auto_save"] = loop.create_task(_auto_save(user.id, row, limit + AUTO_SAVE_GRACE))
    return {
        "ok": True,
        "teaching": True,
        "max_seconds": limit,
        "elapsed": 0,
        "audio_optional": True,
        "records_audio": False,  # only when the user turns the microphone on in the browser
        "agent_id": str(agent_id) if agent_id else None,
    }


def teaching_status(user: User) -> dict:
    row = _TEACH.get(_key(user.id))
    if not row:
        return {"teaching": False, "max_seconds": teach_limit()}
    limit = float(row.get("max_seconds") or teach_limit())
    spent = _elapsed(row)
    return {
        "teaching": True,
        "max_seconds": int(limit),
        "elapsed": round(spent, 1),
        "remaining": round(max(0.0, limit - spent), 1),
        "audio_clips": len(row.get("audio") or []),
        "agent_id": row.get("agent_id"),
    }


async def _transcribe_clip(clip: dict) -> list[dict]:
    try:
        segments = await narration.transcribe(clip["data"], clip["ext"])
    except Exception as exc:  # noqa: BLE001 - narration is optional; the skill still saves
        log.warning("teach: transcription failed: %s: %s", type(exc).__name__, str(exc)[:200])
        clip["error"] = "transcription failed"
        return []
    return narration.place(segments, clip["offset_ms"])


async def add_narration(user: User, data: bytes, content_type: str, filename: str = "", offset_ms: int = 0) -> dict:
    """Store one microphone clip for the running session and start transcribing it right away."""
    row = _TEACH.get(_key(user.id))
    if not row:
        raise SkillError("No teaching session is running.")
    clips = row.setdefault("audio", [])
    if len(clips) >= MAX_AUDIO_CLIPS:
        raise SkillError("Too many audio clips for one teaching session.")
    try:
        ext = narration.check_audio(data, content_type, filename)
    except narration.AudioError as exc:
        raise SkillError(str(exc)) from exc
    limit_ms = int(float(row.get("max_seconds") or teach_limit()) * 1000)
    clip = {"data": data, "ext": ext, "offset_ms": max(0, min(int(offset_ms or 0), limit_ms)), "bytes": len(data)}
    clip["task"] = asyncio.get_running_loop().create_task(_transcribe_clip(clip))
    clips.append(clip)
    return {"ok": True, "clips": len(clips), "bytes": len(data), "format": ext}


def _dedupe_rail_typing(screen: list[dict], rail: list[dict]) -> list[dict]:
    """Typing-bar input also shows up as X key events; keep the exact rail copy."""
    spans = []
    for ev in rail:
        if ev.get("kind") == "input":
            t = float(ev.get("t") or 0)
            spans.append((t - 1.0, t + 3.0 + len(str(ev.get("text") or "")) / 40.0))
    if not spans:
        return screen
    return [
        ev
        for ev in screen
        if ev.get("kind") not in {"type", "key"} or not any(a <= float(ev.get("t") or 0) <= b for a, b in spans)
    ]


SKILL_WRITER = """You turn a recorded demonstration on a computer into a reusable skill for an AI agent that controls the same kind of desktop (a Linux desktop with a Chromium browser).
You get a timeline of what the user did (clicks with screen coordinates and window titles, typing, keys, scrolls, window changes) and what they said out loud while doing it, plus a few screenshots.
Spoken instructions explain intent: prefer them over raw coordinates. Generalize: name the goal, the inputs that will change between runs (as {placeholders}), the steps in order (which site or app, what to look for on screen, what to click or type), and how to tell it worked.
Describe targets by their visible label or purpose, not by pixel coordinates. Never include passwords, one-time codes, card numbers or other secrets that appear in the typing; write {password} etc. instead.
Reply with JSON: {"name": "<short skill name, max 60 chars>", "instruction": "<the skill, plain text with numbered steps, max 2500 chars>"}"""


def _fallback_instruction(timeline: list[dict], notes: list[str]) -> str:
    said = " ".join(e["text"] for e in timeline if e.get("kind") == "say")
    actions = [e for e in timeline if e.get("kind") != "say"]
    lines = ["Repeat this demonstrated workflow on the computer."]
    if said:
        lines.append(f"What the user said while demonstrating: {said[:1500]}")
    if actions:
        lines.append("Recorded steps:")
        lines.append(narration.timeline_text(actions, 80))
    if notes:
        lines.append("Recording notes: " + "; ".join(notes))
    return "\n".join(lines)[:8000]


async def _write_skill(timeline: list[dict], frames: list[dict], name_hint: str) -> dict | None:
    """LLM: timeline + narration + key frames -> {name, instruction}. None when the model is unavailable."""
    from app.llm import chat_complete, openai_client, reasoning_payload

    if not (get_settings().openai_api_key or "").strip():
        return None
    text = (
        f"Name the user chose: {name_hint or '(none)'}\n\nTimeline (mm:ss since the start):\n"
        + narration.timeline_text(timeline)
    )
    picks = frames if len(frames) <= 6 else [frames[round(i * (len(frames) - 1) / 5)] for i in range(6)]
    raw = ""
    if picks:
        content: list[dict] = [{"type": "input_text", "text": text}]
        for frame in picks:
            content.append({"type": "input_text", "text": f"Screenshot at {narration.clock(frame['t'])}:"})
            content.append(
                {"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.b64encode(frame["jpeg"]).decode()}
            )
        try:
            resp = await asyncio.wait_for(
                openai_client().responses.create(
                    model=get_settings().openai_model,
                    instructions=SKILL_WRITER,
                    input=[{"role": "user", "content": content}],
                    text={"format": {"type": "json_object"}},
                    reasoning=reasoning_payload(),
                    max_output_tokens=1800,
                ),
                timeout=60,
            )
            raw = getattr(resp, "output_text", "") or ""
        except Exception as exc:  # noqa: BLE001 - retry without images
            log.info("teach: skill writer with screenshots failed: %s", type(exc).__name__)
    if not raw:
        resp = await asyncio.wait_for(
            chat_complete(
                [{"role": "system", "content": SKILL_WRITER}, {"role": "user", "content": text}],
                response_format={"type": "json_object"},
                max_tokens=1800,
            ),
            timeout=60,
        )
        raw = resp.choices[0].message.content or ""
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    instruction = str(data.get("instruction") or "").strip()
    if not instruction:
        return None
    return {"name": " ".join(str(data.get("name") or "").split())[:60], "instruction": instruction[:8000]}


async def _finish(user_id, row: dict) -> tuple[list[dict], list[dict], list[str], list[dict]]:
    """Stop the recorder and wait for transcripts: (timeline, frames, notes, transcript)."""
    key = _key(user_id)
    captured = await recorder.stop(key)
    rail = list(row.get("steps") or [])
    screen = _dedupe_rail_typing(captured["events"], rail)
    speech: list[dict] = []
    clips = row.get("audio") or []
    if clips:
        done = await asyncio.gather(*(c["task"] for c in clips if c.get("task")), return_exceptions=True)
        for part in done:
            if isinstance(part, list):
                speech.extend(part)
    notes = list(captured["notes"])
    if any(c.get("error") for c in clips):
        notes.append("some narration could not be transcribed")
    actions = narration.align(narration.merge(screen, rail), speech)
    timeline = narration.merge(actions, speech)[: MAX_STEPS * 2]
    return timeline, captured["frames"], notes, speech


async def _improve_skill(skill_id: uuid.UUID, timeline: list[dict], frames: list[dict], name_hint: str, rename: bool) -> None:
    """Background: rewrite the saved skill's instruction from actions + narration."""
    from app.db import SessionLocal

    try:
        written = await _write_skill(timeline, frames, name_hint)
    except Exception as exc:  # noqa: BLE001
        log.warning("teach: skill writer failed: %s: %s", type(exc).__name__, str(exc)[:200])
        written = None
    if not written:
        return
    async with SessionLocal() as session:
        skill = await session.get(AgentSkill, skill_id)
        if skill is None:
            return
        skill.instruction = written["instruction"]
        if rename and written.get("name"):
            skill.name = written["name"][:160]
        await session.commit()
    log.info("teach: skill %s written from %d events", skill_id, len(timeline))


async def _save_taught(db: AsyncSession, user: Any, row: dict, name: str, background: bool = True) -> AgentSkill:
    elapsed_ms = int(_elapsed(row) * 1000)
    limit_ms = int(float(row.get("max_seconds") or teach_limit()) * 1000)
    chosen = " ".join((name or "").split())
    title = chosen or "Taught workflow"
    agent_id = None
    if row.get("agent_id"):
        try:
            agent_id = uuid.UUID(str(row["agent_id"]))
        except (TypeError, ValueError):
            agent_id = None
    timeline, frames, notes, speech = await _finish(user.id, row)
    skill = await save_skill(
        db,
        user,
        name=title,
        agent_id=agent_id,
        kind="taught",
        steps=timeline,
        instruction=_fallback_instruction(timeline, notes),
        duration_ms=min(elapsed_ms, limit_ms),
    )
    rename = not chosen or chosen.lower() == "taught workflow"
    job = _improve_skill(skill.id, timeline, frames, title, rename)
    if background:
        task = asyncio.get_running_loop().create_task(job)
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)
    else:
        await job
    return skill


_BACKGROUND: set[asyncio.Task] = set()


async def _auto_save(user_id, row: dict, after: float) -> None:
    """Save the skill on the server when the limit (plus grace) passes and the browser never stopped."""
    from types import SimpleNamespace

    from app.db import SessionLocal

    await asyncio.sleep(after)
    key = _key(user_id)
    if _TEACH.get(key) is not row:
        return
    _TEACH.pop(key, None)
    try:
        async with SessionLocal() as session:
            await _save_taught(session, SimpleNamespace(id=user_id), row, "")
        log.info("teach: auto-saved a taught skill at the time limit user=%s", key)
    except Exception as exc:  # noqa: BLE001
        log.warning("teach: auto-save failed user=%s: %s", key, type(exc).__name__)


async def stop_teaching(db: AsyncSession, user: User, name: str = "") -> dict:
    row = _TEACH.pop(_key(user.id), None)
    if not row:
        raise SkillError("No teaching session is running.")
    task = row.get("auto_save")
    if task is not None and task is not asyncio.current_task():
        task.cancel()
    skill = await _save_taught(db, user, row, name)
    out = serialize_skill(skill)
    out["writing"] = bool((get_settings().openai_api_key or "").strip())
    out["narrated"] = bool(row.get("audio"))
    return out
