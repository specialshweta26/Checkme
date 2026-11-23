"""Test scenarios for reply times (docs/ADMIN.md "Test scenarios").

The admin console runs a set of everyday questions through the real agent, as the admin, one after another, each in
its own chat: instant replies, data connectors, mail and calendar, the web, several connectors at once, the workspace.
Each reply is timed like any chat reply (it shows in Reply times), and the run's own list says, per question, how long
it took and where the time went. The questions are data (``scenarios.json``) and the admin can add their own.

Only reads: nothing is confirmed on the admin's behalf, so anything that would send, change or delete stays an
approval card in that chat. One run at a time per admin; Stop ends it after the current question.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

log = logging.getLogger(__name__)

_FILE = Path(__file__).with_name("scenarios.json")
_RUNS: dict[str, dict] = {}  # user id -> the run's state (this process)
_TASKS: set = set()
MAX_CUSTOM = 10


def defaults() -> list[dict]:
    try:
        return json.loads(_FILE.read_text())
    except Exception:  # noqa: BLE001
        log.warning("scenarios.json could not be read", exc_info=True)
        return []


def plan(ids: list[str] | None, custom: list[str] | None) -> list[dict]:
    """The questions to run, in order: the chosen defaults (all when none are chosen) plus the admin's own. A scenario
    with ``after`` (a follow-up) runs in the same chat as that one, so it is kept only if that one runs too."""
    known = defaults()
    chosen = [s for s in known if not ids or s["id"] in ids]
    kept = {s["id"] for s in chosen}
    chosen = [s for s in chosen if not s.get("after") or s["after"] in kept]
    for n, text in enumerate([t.strip() for t in (custom or []) if t and t.strip()][:MAX_CUSTOM], 1):
        chosen.append({"id": f"custom-{n}", "group": "Your questions", "text": text[:1000]})
    return chosen


def state(user_id) -> dict | None:
    return _RUNS.get(str(user_id))


def summary(timing: dict, target_s: float) -> dict:
    from app.admin.latency import breakdown

    b = breakdown(timing)
    return {**{k: b[k] for k in ("total_ms", "model_ms", "model_calls", "connector_ms", "tool_ms", "other_ms", "tools", "slowest_calls", "calls", "remembered")},
            "over_target": b["total_ms"] > target_s * 1000}  # fmt: skip


def start(user_id, ids: list[str] | None, custom: list[str] | None) -> dict:
    current = state(user_id)
    if current and current.get("status") == "running":
        return current
    from app.config import get_settings

    items = plan(ids, custom)
    run = {
        "id": uuid.uuid4().hex[:12],
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "target_s": float(getattr(get_settings(), "reply_target_s", 45) or 45),
        "stop": False,
        "results": [{"id": s["id"], "group": s.get("group", ""), "text": s["text"], "status": "waiting"} for s in items],
    }
    _RUNS[str(user_id)] = run
    task = asyncio.create_task(_run_all(user_id, items, run))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return run


def stop(user_id) -> dict | None:
    run = state(user_id)
    if run and run.get("status") == "running":
        run["stop"] = True
    return run


async def _run_all(user_id, items: list[dict], run: dict) -> None:
    sessions: dict[str, Any] = {}
    try:
        for item, row in zip(items, run["results"]):
            if run["stop"]:
                row["status"] = "skipped"
                continue
            row["status"] = "running"
            try:
                await _one(user_id, item, row, run, sessions)
            except Exception as exc:  # noqa: BLE001 - one question failing doesn't end the run
                log.warning("scenario %s failed", item["id"], exc_info=True)
                row.update(status="error", error=str(exc)[:300])
        run["status"] = "stopped" if run["stop"] else "done"
    finally:
        run["finished_at"] = datetime.now(timezone.utc).isoformat()
        if run["status"] == "running":
            run["status"] = "error"


async def _one(user_id, item: dict, row: dict, run: dict, sessions: dict) -> None:
    from app.agent import cancel as run_cancel
    from app.agent import usage as turn_usage
    from app.agent.deep import run_deep_agent
    from app.agent.meter import METER, over_user_cap
    from app.agents import service as agents_service
    from app.config import get_settings
    from app.db import SessionLocal
    from app.models import ChatMessage, ChatSession, User
    from app.security import random_token

    async with SessionLocal() as db:
        user = await db.get(User, uuid.UUID(str(user_id)))
        if user is None:
            raise RuntimeError("user not found")
        if await over_user_cap(db, user):
            row.update(status="skipped", error="daily cap reached")
            run["stop"] = True
            return
        agent = await agents_service.ensure_default_agent(db, user)
        sess = await db.get(ChatSession, sessions[item["after"]]) if item.get("after") in sessions else None
        history: list[dict] = []
        if sess is None:
            sess = ChatSession(user_id=user.id, title=f"Test: {item['text'][:60]}", agent_id=agent.id)
            db.add(sess)
            await db.flush()
        else:
            rows = (await db.execute(select(ChatMessage).where(ChatMessage.session_id == sess.id).order_by(ChatMessage.id))).scalars().all()
            history = [{"role": m.role, "content": m.content} for m in rows if m.role in ("user", "assistant")]
        sessions[item["id"]] = sess.id
        request_id = random_token(12)
        received = time.monotonic()
        from app.chat import quick

        if quick.instant(item["text"], uploads=False, confirms=False, group=False, resuming=False):
            # Small talk is answered by the chat route without an agent run: time that path, as the user gets it.
            text = quick.reply(item["text"], agent.name, await quick.last_turn_state(db, sess))
            timing = {"reply_ms": int((time.monotonic() - received) * 1000), "kind": "instant"}
            db.add(ChatMessage(session_id=sess.id, user_id=user.id, role="user", content=item["text"], meta={"scenario": item["id"]}, request_id=request_id))
            db.add(ChatMessage(session_id=sess.id, user_id=user.id, role="assistant", content=text,
                               meta={"timing": timing, "scenario": item["id"], "scenario_run": run["id"]}, request_id=request_id))  # fmt: skip
            await db.commit()
            row.update(status="ok", session_id=str(sess.id), agent_id=str(agent.id), reply=text[:300], **summary(timing, run["target_s"]))
            return
        tally, usage_token = turn_usage.start()
        meter_token = METER.set((user.id, "chat"))
        turn = run_cancel.register(request_id, user_id=user.id, kind="turn", session_id=sess.id, request_id=request_id)
        run_token = run_cancel.current.set(turn)
        limit = float(getattr(get_settings(), "scenario_timeout_s", 300) or 300)
        try:
            result = await asyncio.wait_for(
                run_deep_agent(db, user, sess.id, request_id, history, item["text"], [], workspace_agent=agent, client_kind="web"),
                timeout=limit,
            )
            status = "ok"
        except TimeoutError:
            result = {"text": f"Stopped after {int(limit)} s (SCENARIO_TIMEOUT_S).", "timing": turn.timing() if callable(turn.timing) else {}}
            status = "timeout"
        finally:
            run_cancel.current.reset(run_token)
            run_cancel.unregister(request_id, turn)
            turn_usage.end(usage_token)
            METER.reset(meter_token)
        timing = {**(result.get("timing") or {}), "reply_ms": int((time.monotonic() - received) * 1000)}
        if status == "timeout":
            timing["stopped"] = "timeout"
        db.add(ChatMessage(session_id=sess.id, user_id=user.id, role="user", content=item["text"], meta={"scenario": item["id"]},
                           request_id=request_id))  # fmt: skip
        db.add(ChatMessage(session_id=sess.id, user_id=user.id, role="assistant", content=result.get("text") or "",
                           meta={"timing": timing, "scenario": item["id"], "scenario_run": run["id"],
                                 "pending_confirms": result.get("pending_confirms"), "used_tools": result.get("used_tools"),
                                 "usage": turn_usage.summary(tally)},
                           request_id=request_id))  # fmt: skip
        sess.updated_at = datetime.now(timezone.utc)
        await db.commit()
        row.update(status=status, session_id=str(sess.id), agent_id=str(agent.id), reply=(result.get("text") or "")[:300],
                   waiting_approval=bool(result.get("pending_confirms")), **summary(timing, run["target_s"]))  # fmt: skip
