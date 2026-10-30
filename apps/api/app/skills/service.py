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


ACTIVE_STATUSES = {"recording", "writing"}
DEFAULT_NAME = "Taught workflow"


def _iso(value) -> str:
    return value.isoformat() if value else ""


def serialize_skill(row: AgentSkill, full: bool = False) -> dict:
    """A skill for the API and agent tools. Screenshots are never inlined (see ``frame_bytes``)."""
    frames = list(getattr(row, "frames", None) or [])
    out = {
        "id": str(row.id),
        "agent_id": str(row.agent_id) if row.agent_id else None,
        "name": row.name,
        "kind": row.kind,
        "status": getattr(row, "status", None) or "ready",
        "error": getattr(row, "error", None) or "",
        "steps": row.steps or [],
        "instruction": row.instruction or "",
        "duration_ms": row.duration_ms or 0,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(getattr(row, "updated_at", None)),
        "step_count": len(row.steps or []),
        "frame_count": len(frames),
        "has_transcript": bool(getattr(row, "transcript", None)),
    }
    if full:
        out["transcript"] = list(getattr(row, "transcript", None) or [])
        out["notes"] = list(getattr(row, "notes", None) or [])
        out["frames"] = [{"index": i, "t": f.get("t", 0)} for i, f in enumerate(frames) if isinstance(f, dict)]
    return out


async def list_skills(db: AsyncSession, user: User, agent_id: uuid.UUID | None = None) -> list[dict]:
    stmt = select(AgentSkill).where(AgentSkill.user_id == user.id).order_by(AgentSkill.created_at.desc())
    if agent_id:
        stmt = stmt.where(or_(AgentSkill.agent_id == agent_id, AgentSkill.agent_id.is_(None)))
    try:
        await housekeep(db, user)
    except Exception as exc:  # noqa: BLE001 - listing must still work
        log.info("skills: housekeeping failed: %s", type(exc).__name__)
        await db.rollback()
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


# --- Taught skills: saved while recording -------------------------------------------------------------
#
# A teaching session creates the skill row right away (status "recording") and a flush loop writes the
# recorded steps, narration transcript and key screenshots into it every few seconds, so nothing is lost if
# the API restarts or the browser drops. Stop marks it "writing" and returns at once; a background job
# finishes the recording, then writes the instruction with the LLM (timeouts + retries) or, when that fails,
# with a deterministic step list. The row ends "ready" (``error`` explains a failed write-up; Retry re-runs it).

FLUSH_EVERY = 5.0
WRITER_TIMEOUT = 75.0
WRITER_ATTEMPTS = 2
TRANSCRIBE_WAIT = 90.0
STALE_WRITING_SECONDS = 180.0
ORPHAN_RECORDING_SECONDS = 60.0
_JOBS: dict[str, asyncio.Task] = {}
_BACKGROUND: set[asyncio.Task] = set()


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)


def _spawn(coro, key: str | None = None) -> asyncio.Task:
    task = asyncio.get_running_loop().create_task(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    if key:
        _JOBS[key] = task
        task.add_done_callback(lambda _t, k=key: _JOBS.pop(k, None) if _JOBS.get(k) is _t else None)
    return task


# Persistence helpers (own sessions so background jobs never share a request's session). Tests replace them.
async def _store_create(user_id, agent_id, name: str, auto_named: bool) -> str | None:
    from app.db import SessionLocal

    async with SessionLocal() as session:
        row = AgentSkill(
            user_id=uuid.UUID(str(user_id)),
            agent_id=agent_id,
            name=name[:160],
            kind="taught",
            steps=[],
            instruction="",
            duration_ms=0,
            status="recording",
            error="",
            transcript=[],
            frames=[],
            notes=[],
            auto_named=auto_named,
            updated_at=_now(),
        )
        session.add(row)
        await session.commit()
        return str(row.id)


async def _store_update(skill_id: str, **fields: Any) -> bool:
    from app.db import SessionLocal

    async with SessionLocal() as session:
        row = await session.get(AgentSkill, uuid.UUID(str(skill_id)))
        if row is None:
            return False
        for key, value in fields.items():
            setattr(row, key, value)
        row.updated_at = _now()
        await session.commit()
        return True


async def _store_get(skill_id: str) -> dict | None:
    from app.db import SessionLocal

    async with SessionLocal() as session:
        row = await session.get(AgentSkill, uuid.UUID(str(skill_id)))
        if row is None:
            return None
        return {
            "id": str(row.id),
            "user_id": str(row.user_id),
            "name": row.name,
            "steps": list(row.steps or []),
            "transcript": list(row.transcript or []),
            "frames": list(row.frames or []),
            "notes": list(row.notes or []),
            "auto_named": bool(row.auto_named),
            "status": row.status or "ready",
            "instruction": row.instruction or "",
        }


async def _store_delete(skill_id: str) -> None:
    from app.db import SessionLocal

    async with SessionLocal() as session:
        row = await session.get(AgentSkill, uuid.UUID(str(skill_id)))
        if row is not None:
            await session.delete(row)
            await session.commit()


async def _store_find_recording(user_id) -> str | None:
    from app.db import SessionLocal

    async with SessionLocal() as session:
        stmt = (
            select(AgentSkill.id)
            .where(AgentSkill.user_id == uuid.UUID(str(user_id)), AgentSkill.status == "recording")
            .order_by(AgentSkill.created_at.desc())
        )
        found = (await session.execute(stmt)).scalars().first()
        return str(found) if found else None


# --- Secrets in typed text --------------------------------------------------------------------------------

_SECRET_CONTEXT = re.compile(r"pass(word|code|phrase)?|\bpin\b|otp|one[- ]time|verification|2fa|mfa|secret|cvv|cvc|security code|card number|api key|token", re.I)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)


def looks_secret(text: str) -> bool:
    """Typed text that is probably a credential: OTP/PIN digits, card numbers, or password-like strings."""
    value = (text or "").strip()
    digits = re.sub(r"[\s-]", "", value)
    if digits.isdigit() and (6 <= len(digits) <= 8 or 13 <= len(digits) <= 19):
        return True
    if not value or " " in value or _EMAIL.match(value) or value.startswith(("http://", "https://", "www.")):
        return False
    if len(value) < 8:
        return False
    classes = sum(bool(re.search(p, value)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
    return classes >= 3


def mask_secrets(timeline: list[dict]) -> list[dict]:
    """Copy of the timeline with likely secrets in typed text replaced. Raw secrets are never stored."""
    out: list[dict] = []
    context = ""
    for ev in timeline:
        row = dict(ev)
        if row.get("window"):
            context = str(row["window"])
        if row.get("kind") in {"type", "input"} and row.get("text"):
            text = str(row["text"])
            hint = f"{context} {row.get('said') or ''}"
            if looks_secret(text) or (_SECRET_CONTEXT.search(hint) and " " not in text.strip()):
                row["text"] = f"•••• (hidden, {len(text)} characters)"
                row["masked"] = True
        if row.get("kind") == "control" and row.get("text") and looks_secret(str(row["text"])):
            row["text"] = "•••• (hidden)"
            row["masked"] = True
        out.append(row)
    return out


# --- Teaching ----------------------------------------------------------------------------------------------


async def start_teaching(user: User, agent_id: uuid.UUID | None = None, name: str = "") -> dict:
    """Record a demonstration on the computer for up to ``teach_limit()`` seconds.

    The skill is created immediately (status "recording") and filled in as the user works: steps, narration
    clips (``add_narration``, transcribed as they arrive) and key screenshots.
    """
    key = _key(user.id)
    old = _TEACH.get(key)
    if old:
        # A second start while one is running: save the old one first instead of dropping it.
        _TEACH.pop(key, None)
        _cancel_session_tasks(old)
        if old.get("skill_id"):
            _spawn(_finalize(key, old), key=old["skill_id"])
    limit = teach_limit()
    chosen = " ".join((name or "").split())
    row: dict[str, Any] = {
        "agent_id": str(agent_id) if agent_id else None,
        "started_at": time.monotonic(),
        "started_wall": time.time(),
        "max_seconds": limit,
        "steps": [],
        "audio": [],
        "name": chosen,
        "skill_id": None,
        "flushed": {},
    }
    _TEACH[key] = row
    try:
        row["skill_id"] = await _store_create(user.id, agent_id, chosen or DEFAULT_NAME, auto_named=not chosen)
    except Exception as exc:  # noqa: BLE001 - keep recording in memory; the skill is created at stop
        log.warning("teach: could not create the draft skill user=%s: %s", key, type(exc).__name__)
    recorder.start(key, row["started_at"], limit)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        row["auto_save"] = loop.create_task(_auto_save(user.id, row, limit + AUTO_SAVE_GRACE))
        row["flusher"] = loop.create_task(_flush_loop(key, row))
    return {
        "ok": True,
        "teaching": True,
        "max_seconds": limit,
        "elapsed": 0,
        "audio_optional": True,
        "records_audio": False,  # only when the user turns the microphone on in the browser
        "agent_id": str(agent_id) if agent_id else None,
        "skill_id": row["skill_id"],
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
        "skill_id": row.get("skill_id"),
        "steps": len(row.get("steps") or []),
    }


async def _transcribe_clip(clip: dict) -> list[dict]:
    try:
        segments = await narration.transcribe(clip["data"], clip["ext"])
    except Exception as exc:  # noqa: BLE001 - narration is optional; the skill still saves
        log.warning("teach: transcription failed: %s: %s", type(exc).__name__, str(exc)[:200])
        clip["error"] = "transcription failed"
        return []
    placed = narration.place(segments, clip["offset_ms"])
    clip["speech"] = placed
    clip.pop("data", None)  # transcribed: no need to keep the audio in memory
    log.info("teach: transcribed a narration clip (%d segments)", len(placed))
    return placed


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
    log.info("teach: narration clip %d received (%d bytes, %s)", len(clips), len(data), ext)
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


def _speech_so_far(row: dict) -> list[dict]:
    speech: list[dict] = []
    for clip in row.get("audio") or []:
        speech.extend(clip.get("speech") or [])
    return sorted(speech, key=lambda s: float(s.get("t") or 0))


def _build_timeline(screen: list[dict], rail: list[dict], speech: list[dict]) -> list[dict]:
    """Merged, narration-aligned, secret-masked timeline (what gets stored and shown)."""
    screen = _dedupe_rail_typing(screen, rail)
    actions = narration.align(narration.merge(screen, rail), speech)
    return mask_secrets(narration.merge(actions, speech))[: MAX_STEPS * 2]


def _frames_payload(frames: list[dict]) -> list[dict]:
    return [
        {"t": f.get("t", 0), "jpeg": base64.b64encode(f["jpeg"]).decode()}
        for f in frames
        if isinstance(f, dict) and f.get("jpeg")
    ]


async def _flush(key: str, row: dict) -> None:
    """Write what has been recorded so far into the draft skill."""
    skill_id = row.get("skill_id")
    if not skill_id:
        return
    seen = recorder.peek(key)
    speech = _speech_so_far(row)
    steps = _build_timeline(seen["events"], list(row.get("steps") or []), speech)
    fields: dict[str, Any] = {}
    sig = (len(steps), steps[-1].get("t") if steps else None, len(speech), len(seen["notes"]))
    if row["flushed"].get("sig") != sig:
        fields.update(steps=steps, transcript=speech, notes=seen["notes"])
        row["flushed"]["sig"] = sig
    frame_sig = tuple(f.get("t") for f in seen["frames"])
    if row["flushed"].get("frames") != frame_sig:
        fields["frames"] = _frames_payload(seen["frames"])
        row["flushed"]["frames"] = frame_sig
    fields["duration_ms"] = int(_elapsed(row) * 1000)
    await _store_update(skill_id, **fields)


async def _flush_loop(key: str, row: dict) -> None:
    while _TEACH.get(key) is row:
        await asyncio.sleep(FLUSH_EVERY)
        if _TEACH.get(key) is not row:
            return
        try:
            await _flush(key, row)
        except Exception as exc:  # noqa: BLE001 - try again next tick
            log.info("teach: flush failed user=%s: %s", key, type(exc).__name__)


def _cancel_session_tasks(row: dict) -> None:
    current = None
    try:
        current = asyncio.current_task()
    except RuntimeError:
        pass
    for name in ("auto_save", "flusher"):
        task = row.get(name)
        if task is not None and task is not current:
            task.cancel()


SKILL_WRITER = """You turn a recorded demonstration on a computer into a reusable skill for an AI agent that controls the same kind of desktop (a Linux desktop with a Chromium browser).
You get a timeline of what the user did (clicks with screen coordinates and window titles, typing, keys, scrolls, window changes) and what they said out loud while doing it, plus a few screenshots.
Spoken instructions explain intent: prefer them over raw coordinates. Generalize: name the goal, the inputs that will change between runs (as {placeholders}), the steps in order (which site or app, what to look for on screen, what to click or type), and how to tell it worked.
Describe targets by their visible label or purpose, not by pixel coordinates. Never include passwords, one-time codes, card numbers or other secrets that appear in the typing; write {password} etc. instead.
Reply with JSON: {"name": "<short skill name, max 60 chars>", "instruction": "<the skill, plain text with numbered steps, max 2500 chars>"}"""

# The Responses API only allows json_object output when the *input* mentions JSON (instructions don't count).
WRITER_ASK = 'Reply with a JSON object {"name": ..., "instruction": ...} as described.'


def auto_name(timeline: list[dict], transcript: list[dict] | None = None) -> str:
    """Deterministic name from what was recorded: the first spoken sentence or the main window/site."""
    for seg in transcript or []:
        words = str(seg.get("text") or "").strip().split()
        if len(words) >= 2:
            return " ".join(words[:8]).rstrip(".,!?")[:60]
    titles: dict[str, int] = {}
    for ev in timeline:
        title = str(ev.get("window") or "").strip()
        if title:
            short = re.split(r"\s+[-–—|]\s+", title)[0].strip()[:50]
            if short and short.lower() not in {"chromium", "google chrome", "new tab"}:
                titles[short] = titles.get(short, 0) + 1
    if titles:
        return f"Workflow: {max(titles, key=titles.get)}"[:60]
    typed = [str(ev.get("text")) for ev in timeline if ev.get("kind") in {"type", "input"} and ev.get("text") and not ev.get("masked")]
    if typed:
        return f"Workflow: {typed[0][:40]}"
    return DEFAULT_NAME


def step_list_instruction(timeline: list[dict], transcript: list[dict], notes: list[str]) -> str:
    """Deterministic skill text: what the user said, then every recorded step in order."""
    said = " ".join(str(s.get("text") or "") for s in transcript).strip()
    lines = ["Repeat this demonstrated workflow on the computer. Recheck the screen before each step; targets may have moved."]
    if said:
        lines.append(f"What the user said while demonstrating: {said[:2000]}")
    if timeline:
        lines.append("Recorded steps:")
        for i, ev in enumerate(timeline[:150], 1):
            line = f"{i}. [{narration.clock(float(ev.get('t') or 0))}] {narration.describe(ev)}"
            if ev.get("said"):
                line += f" — user said: “{ev['said']}”"
            lines.append(line)
        if len(timeline) > 150:
            lines.append(f"… {len(timeline) - 150} more steps (see the skill's steps).")
    else:
        lines.append("No on-screen steps were captured.")
    if notes:
        lines.append("Recording notes: " + "; ".join(dict.fromkeys(notes)))
    return "\n".join(lines)[:8000]


def _parse_writer(raw: str) -> dict | None:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        data = json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None
    instruction = str(data.get("instruction") or "").strip()
    if not instruction:
        return None
    return {"name": " ".join(str(data.get("name") or "").split())[:60], "instruction": instruction[:8000]}


async def _write_skill(timeline: list[dict], frames: list[dict], name_hint: str, transcript: list[dict] | None = None) -> dict:
    """LLM: timeline + narration + key frames -> {name, instruction}. Raises the last error when every attempt fails."""
    from app.llm import chat_complete, openai_client, reasoning_payload

    if not (get_settings().openai_api_key or "").strip():
        raise RuntimeError("no OpenAI key is configured")
    speech = list(transcript or [])
    text = (
        f"Name the user chose: {name_hint or '(none)'}\n\nTimeline (mm:ss since the start):\n"
        + narration.timeline_text(narration.merge(timeline, speech))
        + f"\n\n{WRITER_ASK}"
    )
    picks = frames if len(frames) <= 6 else [frames[round(i * (len(frames) - 1) / 5)] for i in range(6)]
    last: Exception | None = None
    for attempt in range(WRITER_ATTEMPTS):
        if picks:
            content: list[dict] = [{"type": "input_text", "text": text}]
            for frame in picks:
                content.append({"type": "input_text", "text": f"Screenshot at {narration.clock(frame.get('t', 0))}:"})
                content.append({"type": "input_image", "image_url": "data:image/jpeg;base64," + frame["jpeg"]})
            try:
                resp = await asyncio.wait_for(
                    openai_client().responses.create(
                        model=get_settings().openai_model,
                        instructions=SKILL_WRITER,
                        input=[{"role": "user", "content": content}],
                        text={"format": {"type": "json_object"}},
                        reasoning=reasoning_payload(),
                        max_output_tokens=2400,
                    ),
                    timeout=WRITER_TIMEOUT,
                )
                written = _parse_writer(getattr(resp, "output_text", "") or "")
                if written:
                    return written
                last = ValueError("the model reply was not a usable skill")
            except Exception as exc:  # noqa: BLE001 - retry, then without images
                last = exc
                log.info("teach: skill writer with screenshots failed (attempt %d): %s: %s", attempt + 1, type(exc).__name__, str(exc)[:200])
        try:
            resp = await asyncio.wait_for(
                chat_complete(
                    [{"role": "system", "content": SKILL_WRITER}, {"role": "user", "content": text}],
                    response_format={"type": "json_object"},
                    max_tokens=2400,
                ),
                timeout=WRITER_TIMEOUT,
            )
            written = _parse_writer(resp.choices[0].message.content or "")
            if written:
                return written
            last = ValueError("the model reply was not a usable skill")
        except Exception as exc:  # noqa: BLE001
            last = exc
            log.info("teach: skill writer failed (attempt %d): %s: %s", attempt + 1, type(exc).__name__, str(exc)[:200])
        await asyncio.sleep(2 * (attempt + 1))
    raise last or RuntimeError("skill writer failed")


def _short_error(exc: Exception) -> str:
    text = str(exc) or type(exc).__name__
    return f"{type(exc).__name__}: {text}"[:300]


async def write_up(skill_id: str) -> None:
    """Background: write the skill's instruction from its stored steps, narration and screenshots.

    Always leaves the skill "ready": with the LLM write-up, or with the deterministic step list plus ``error``.
    """
    data = await _store_get(skill_id)
    if data is None:
        return
    steps, transcript, notes = data["steps"], data["transcript"], data["notes"]
    actions = [e for e in steps if e.get("kind") != "say"]
    fallback_name = auto_name(actions, transcript)
    try:
        written = await _write_skill(actions, data["frames"], "" if data["auto_named"] else data["name"], transcript)
    except Exception as exc:  # noqa: BLE001 - deterministic skill instead
        log.warning("teach: skill writer failed skill=%s: %s", skill_id, _short_error(exc))
        fields: dict[str, Any] = {
            "instruction": step_list_instruction(actions, transcript, notes),
            "status": "ready",
            "error": f"The AI write-up failed ({_short_error(exc)}). Saved the recorded steps as the instructions; you can retry the write-up.",
        }
        if data["auto_named"] and fallback_name != DEFAULT_NAME:
            fields.update(name=fallback_name)
        await _store_update(skill_id, **fields)
        return
    fields = {"instruction": written["instruction"], "status": "ready", "error": ""}
    if data["auto_named"]:
        fields.update(name=(written.get("name") or fallback_name)[:160], auto_named=False)
    await _store_update(skill_id, **fields)
    log.info("teach: skill %s written from %d steps and %d narration segments", skill_id, len(actions), len(transcript))


async def _finalize(key: str, row: dict) -> None:
    """Background: stop the recorder, wait for transcripts, store the final recording, then write it up."""
    skill_id = row.get("skill_id")
    captured: dict = {}
    try:
        captured = await recorder.stop(key)
        clips = row.get("audio") or []
        tasks = [c["task"] for c in clips if c.get("task")]
        if tasks:
            await asyncio.wait(tasks, timeout=TRANSCRIBE_WAIT)
        speech = _speech_so_far(row)
        notes = list(captured["notes"])
        if any(c.get("error") for c in clips) or any(not t.done() for t in tasks):
            notes.append("some narration could not be transcribed")
        steps = _build_timeline(captured["events"], list(row.get("steps") or []), speech)
        frames = _frames_payload(captured["frames"])
        duration = int(min(_elapsed(row), float(row.get("max_seconds") or teach_limit())) * 1000)
        if not skill_id:
            skill_id = await _store_create(row["user_id"], row.get("agent_uuid"), row.get("name") or DEFAULT_NAME, not row.get("name"))
            row["skill_id"] = skill_id
        if not steps and not speech and not frames:
            await _store_delete(skill_id)
            log.info("teach: nothing was recorded; removed empty skill %s", skill_id)
            return
        await _store_update(
            skill_id, steps=steps, transcript=speech, frames=frames, notes=notes, duration_ms=duration, status="writing",
            instruction=step_list_instruction([e for e in steps if e.get("kind") != "say"], speech, notes),
        )  # fmt: skip
    except Exception as exc:  # noqa: BLE001 - whatever was flushed while recording is still saved
        log.warning("teach: finishing the recording failed skill=%s: %s", skill_id, _short_error(exc))
        if not skill_id:
            return
    trace_id = await _save_dom_trace(row, skill_id, (captured or {}).get("dom_events") or [])
    await write_up(skill_id)
    if trace_id:
        await _add_replay_line(skill_id)


async def _save_dom_trace(row: dict, skill_id: str, dom_events: list[dict]) -> str | None:
    """BROWSER_TEACH_DOM: the page browser's selector-level steps become a replayable trace for this skill."""
    if not dom_events or not recorder.teach_dom_enabled():
        return None
    try:
        from app.agent import fast_browser

        trace_id = await fast_browser.save_teach_trace(row["user_id"], str(skill_id), row.get("name") or "", dom_events)
        log.info("teach: saved %d DOM events as replay trace for skill=%s: %s", len(dom_events), skill_id, bool(trace_id))
        return trace_id
    except Exception as exc:  # noqa: BLE001 - the skill itself is still written
        log.info("teach: saving the replay trace failed skill=%s: %s", skill_id, type(exc).__name__)
        return None


async def _add_replay_line(skill_id: str) -> None:
    try:
        from app.agent import fast_browser

        data = await _store_get(skill_id)
        line = fast_browser.teach_replay_line(str(skill_id))
        if data and line.strip() not in (data.get("instruction") or ""):
            await _store_update(skill_id, instruction=(data.get("instruction") or "") + line)
    except Exception as exc:  # noqa: BLE001
        log.info("teach: adding the replay hint failed skill=%s: %s", skill_id, type(exc).__name__)


async def _finalize_orphan(skill_id: str) -> None:
    """A recording whose session was lost (API restart): its flushed steps are the recording."""
    data = await _store_get(skill_id)
    if data is None:
        return
    steps, transcript = data["steps"], data["transcript"]
    if not steps and not transcript and not data["frames"]:
        await _store_delete(skill_id)
        return
    notes = list(data["notes"]) + ["the recording was cut short (the server restarted); steps up to then were saved"]
    actions = [e for e in steps if e.get("kind") != "say"]
    await _store_update(skill_id, status="writing", notes=notes, instruction=step_list_instruction(actions, transcript, notes))
    await write_up(skill_id)


async def _auto_save(user_id, row: dict, after: float) -> None:
    """Save the skill on the server when the limit (plus grace) passes and the browser never stopped."""
    await asyncio.sleep(after)
    key = _key(user_id)
    if _TEACH.get(key) is not row:
        return
    _TEACH.pop(key, None)
    _cancel_session_tasks(row)
    row.setdefault("user_id", user_id)
    log.info("teach: time limit reached, saving user=%s", key)
    await _finalize(key, row)


async def stop_teaching(db: AsyncSession | None, user: User, name: str = "") -> dict:
    """Stop recording and return the saved skill right away (status "writing"); the write-up runs in the background."""
    key = _key(user.id)
    chosen = " ".join((name or "").split())
    custom = bool(chosen) and chosen.lower() != DEFAULT_NAME.lower()
    row = _TEACH.pop(key, None)
    if row is None:
        skill_id = await _store_find_recording(user.id)
        if not skill_id:
            raise SkillError("No teaching session is running.")
        fields: dict[str, Any] = {"status": "writing"}
        if custom:
            fields.update(name=chosen[:160], auto_named=False)
        await _store_update(skill_id, **fields)
        _spawn(_finalize_orphan(skill_id), key=skill_id)
        data = await _store_get(skill_id) or {}
        return _stop_reply(skill_id, data.get("name") or chosen or DEFAULT_NAME, row, data.get("steps") or [], recovered=True)
    _cancel_session_tasks(row)
    row["user_id"] = user.id
    row["agent_uuid"] = uuid.UUID(row["agent_id"]) if row.get("agent_id") else None
    if custom:
        row["name"] = chosen
    skill_id = row.get("skill_id")
    if skill_id:
        fields = {"status": "writing", "duration_ms": int(_elapsed(row) * 1000)}
        if custom:
            fields.update(name=chosen[:160], auto_named=False)
        try:
            await _store_update(skill_id, **fields)
        except Exception as exc:  # noqa: BLE001 - the background job retries the write
            log.warning("teach: marking skill writing failed: %s", type(exc).__name__)
    _spawn(_finalize(key, row), key=skill_id or f"pending:{key}")
    return _stop_reply(skill_id, chosen if custom else DEFAULT_NAME, row, row.get("steps") or [])


def _stop_reply(skill_id, name: str, row: dict | None, steps: list, recovered: bool = False) -> dict:
    return {
        "id": skill_id,
        "name": name,
        "kind": "taught",
        "status": "writing",
        "error": "",
        "writing": True,
        "narrated": bool(row and row.get("audio")),
        "step_count": len(steps),
        "recovered": recovered,
    }


async def rewrite_skill(db: AsyncSession, user: User, skill_id: str) -> dict:
    """Re-run the write-up for a taught skill (Retry)."""
    row = await get_skill(db, user, skill_id)
    if row is None:
        raise SkillError("Unknown skill.")
    if not (row.steps or row.transcript):
        raise SkillError("This skill has no recorded steps to write up.")
    key = str(row.id)
    if key in _JOBS:
        return serialize_skill(row)
    row.status = "writing"
    row.error = ""
    row.updated_at = _now()
    await db.commit()
    await db.refresh(row)
    _spawn(write_up(key), key=key)
    return serialize_skill(row)


async def frame_bytes(db: AsyncSession, user: User, skill_id: str, index: int) -> bytes | None:
    row = await get_skill(db, user, skill_id)
    frames = list((row.frames if row is not None else None) or [])
    if not 0 <= index < len(frames) or not isinstance(frames[index], dict):
        return None
    try:
        return base64.b64decode(frames[index].get("jpeg") or "")
    except (ValueError, TypeError):
        return None


async def housekeep(db: AsyncSession, user: User) -> None:
    """Repair taught skills left mid-flight by a restart, and drop empty default-named duplicates."""
    from datetime import timedelta

    stmt = select(AgentSkill).where(AgentSkill.user_id == user.id, AgentSkill.kind == "taught")
    rows = (await db.execute(stmt)).scalars().all()
    now = _now()
    live = {str(r.get("skill_id")) for r in _TEACH.values() if r.get("skill_id")}
    changed = False
    for row in rows:
        sid = str(row.id)
        status = row.status or "ready"
        age = now - (row.updated_at or row.created_at or now)
        if status == "recording" and sid not in live and sid not in _JOBS and age > timedelta(seconds=ORPHAN_RECORDING_SECONDS):
            row.status = "writing"
            row.updated_at = now
            changed = True
            _spawn(_finalize_orphan(sid), key=sid)
        elif status == "writing" and sid not in _JOBS and age > timedelta(seconds=STALE_WRITING_SECONDS):
            row.status = "ready"
            row.error = "The write-up was interrupted. The recorded steps are saved; retry the write-up."
            if not (row.instruction or "").strip():
                row.instruction = step_list_instruction(
                    [e for e in row.steps or [] if e.get("kind") != "say"], list(row.transcript or []), list(row.notes or [])
                )
            row.updated_at = now
            changed = True
        elif (
            status == "ready"
            and (row.name or "").strip().lower() == DEFAULT_NAME.lower()
            and not row.steps
            and not row.transcript
            and not row.frames
            and not (row.instruction or "").strip()
        ):
            await db.delete(row)  # an empty taught skill from an interrupted session: nothing to keep
            changed = True
    if changed:
        await db.commit()
