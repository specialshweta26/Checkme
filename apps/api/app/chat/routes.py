from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone

import anyio
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse

log = logging.getLogger(__name__)
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import cancel as run_cancel
from app.agent.attachments import summarize_one
from app.agent.confirm import read_confirm
from app.agent.deep import run_deep_agent
from app.agent.memory import seed_letta
from app.agent.progress import ProgressReporter, read_progress, read_progress_draft, read_progress_session, valid_progress_id
from app.agents import service as agents_service
from app.cache import rate_limited
from app.config import get_settings
from app.connectors.runtime import RuntimeConnector
from app.db import get_db
from app.deps import current_user
from app.models import ChatMessage, ChatSession, Connector, User
from app.security import random_token

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatIn(BaseModel):
    message: str
    session_id: uuid.UUID | None = None
    confirmations: list[dict] = []
    attachments: list[dict] = []
    agent_id: uuid.UUID | None = None
    group_id: uuid.UUID | None = None
    progress_id: str | None = None
    # While a turn runs in this chat: "interrupt" (stop it and handle this message, the default) or "queue" (wait).
    mode: str | None = None
    # Continue a stopped turn: the request_id of its "Stopped" message.
    resume_request_id: str | None = None


class CancelIn(BaseModel):
    request_id: str | None = None
    progress_id: str | None = None
    session_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None


@router.get("/progress/{progress_id}")
async def chat_progress(progress_id: str, user: User = Depends(current_user)):
    """Live status lines for an in-flight chat turn (polled by the web app)."""
    if not valid_progress_id(progress_id):
        raise HTTPException(400, "Bad progress id")
    steps = await read_progress(user.id, progress_id)
    session_id = await read_progress_session(user.id, progress_id)
    draft = await read_progress_draft(user.id, progress_id)
    return {
        "steps": steps,
        "done": bool(steps and steps[-1].get("kind") == "done"),
        "session_id": session_id or None,
        # The reply streaming in, so the web app can show text before the turn finishes.
        "draft": draft,
    }


@router.get("/sessions")
async def sessions(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(ChatSession).where(ChatSession.user_id == user.id).order_by(ChatSession.updated_at.desc()).limit(30)
        )
    ).scalars().all()
    return {
        "sessions": [
            {
                "id": str(s.id),
                "title": s.title,
                "updated_at": s.updated_at.isoformat(),
                "agent_id": str(s.agent_id) if s.agent_id else None,
                "group_id": str(s.group_id) if s.group_id else None,
            }
            for s in rows
        ]
    }


@router.get("/sessions/{session_id}")
async def session_messages(session_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    sess = await db.get(ChatSession, session_id)
    if sess is None or sess.user_id != user.id:
        raise HTTPException(404, "Not found")
    rows = (
        await db.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.id)
            .limit(200)
        )
    ).scalars().all()
    return {
        "session": {
            "id": str(sess.id),
            "title": sess.title,
            "agent_id": str(sess.agent_id) if sess.agent_id else None,
            "group_id": str(sess.group_id) if sess.group_id else None,
        },
        "messages": [{"role": m.role, "content": m.content, "meta": m.meta} for m in rows if m.role in ("user", "assistant")],
    }


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    sess = await db.get(ChatSession, session_id)
    if sess is None or sess.user_id != user.id:
        raise HTTPException(404, "Not found")
    await db.delete(sess)
    await db.commit()
    return {"ok": True}


@router.post("")
async def chat(
    body: ChatIn,
    background: BackgroundTasks,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    if await rate_limited(f"chat:{user.id}", get_settings().chat_rate_limit_per_minute):
        raise HTTPException(429, "Too many messages. Wait a minute.")
    text = (body.message or "").strip()
    uploads = [item for item in (body.attachments or []) if isinstance(item, dict) and item.get("key")]
    if not text and not uploads:
        raise HTTPException(400, "Empty message")
    if not text:
        text = "Please review the attached files."
    agent_text = text
    if uploads:
        bits = []
        keys = []
        for item in uploads[:8]:
            keys.append(item.get("key"))
            name = item.get("filename") or "file"
            extracted = (item.get("extracted_text") or "").strip()
            bits.append(f"- {name} (attachment_key={item.get('key')})" + (f"\n{extracted[:3000]}" if extracted else ""))
        agent_text = (
            text
            + "\n\nThe user uploaded these files. Use attachment_keys when sending or replying to mail. "
            "Analyze them in detail; do not give a brief summary.\n"
            + "\n".join(bits)
        )

    try:
        workspace_agent, group, teammates = await _resolve_roster(db, user, body.agent_id, body.group_id)
    except agents_service.AgentError as exc:
        raise HTTPException(400, str(exc)) from exc

    mentions: dict = {}
    want_agent = None if group is not None else workspace_agent.id
    want_group = group.id if group is not None else None
    sess = None
    if body.session_id:
        found = await db.get(ChatSession, body.session_id)
        if found is None or found.user_id != user.id:
            raise HTTPException(404, "Unknown session")
        if agents_service.session_belongs(found, agent_id=want_agent, group_id=want_group):
            sess = found
    if sess is None:
        sess = ChatSession(
            user_id=user.id,
            title=text[:80],
            state={},
            agent_id=want_agent,
            group_id=want_group,
        )
        db.add(sess)
        await db.flush()

    interrupted = await _settle_running(user, sess, body.mode)
    if interrupted:
        agent_text = run_cancel.resume_prompt(
            interrupted.get("text") or "", interrupted.get("partial") or "", interrupted.get("steps") or [], interrupted_by=text
        ) + f"\n\nNew message from the user:\n{agent_text}"
    if body.resume_request_id:
        resumed = await _resume_context(db, sess, body.resume_request_id)
        if resumed:
            agent_text = run_cancel.resume_prompt(resumed["text"], resumed["partial"], resumed["steps"]) + (
                f"\n\nThe user says: {text}" if text.strip().lower() not in {"continue", "continue."} else ""
            )

    if not user.letta_agent_id:
        background.add_task(seed_letta, user.id)

    request_id = random_token(12)
    public_uploads = [
        {k: item.get(k) for k in ("key", "filename", "content_type", "kind", "download_url", "preview_url", "previewable", "size") if item.get(k) is not None}
        for item in uploads
    ]
    history_rows = (
        await db.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == sess.id, ChatMessage.role.in_(("user", "assistant")))
            .order_by(ChatMessage.id.desc())
            .limit(get_settings().history_messages)
        )
    ).scalars().all()
    history = []
    for m in reversed(history_rows):
        content = m.content
        src = (m.meta or {}).get("source") if isinstance(m.meta, dict) else None
        if m.role == "assistant" and src:
            content = f"{content}\n\nTool evidence (cite source_tool if asked): {json.dumps(src, default=str)[:2000]}"
        history.append({"role": m.role, "content": content})

    confirms = []
    for item in body.confirmations:
        token = item.get("token")
        if not token:
            continue
        data = read_confirm(token)
        if data and data.get("kind") == "tool":
            confirms.append(data["payload"])

    working = dict(sess.state or {})
    if confirms:
        working["confirm_tools"] = confirms
    if group is not None and teammates:
        from app.agents.collab import is_group_hello, is_kickoff, is_roll_call, parse_mentions, pick_owner

        mentions = parse_mentions(text, teammates)
        if is_kickoff(history) and (is_group_hello(text) or mentions.get("everyone")):
            mentions["kickoff"] = True
            mentions["roll_call"] = True
        elif mentions.get("everyone") or is_roll_call(text):
            mentions["roll_call"] = True
        owner = pick_owner(teammates, group, working, mentions)
        if owner is not None:
            workspace_agent = owner
            working["owner_id"] = str(owner.id)

    # #region agent log
    from app.debug_ndjson import dbg

    dbg("B", "chat/routes.py:chat", "chat request", {"query": text[:80], "has_session": bool(body.session_id)})
    # #endregion
    t0 = time.time()
    routed = await _maybe_start_job(db, user, sess, workspace_agent, group, confirms, text, agent_text, history, request_id)
    if routed is not None:
        return routed
    progress = ProgressReporter(user.id, body.progress_id) if valid_progress_id(body.progress_id) else None
    working_message = await _start_turn(
        db,
        sess,
        user.id,
        request_id,
        text,
        user_meta={"uploads": public_uploads} if public_uploads else None,
        progress_id=body.progress_id if progress is not None else None,
        agent=workspace_agent,
        group=group,
    )
    if progress is not None:
        await progress.set_session(sess.id)

    run = run_cancel.register(
        request_id,
        user_id=user.id,
        kind="turn",
        session_id=sess.id,
        agent_id=getattr(workspace_agent, "id", None),
        request_id=request_id,
        progress_id=body.progress_id if progress is not None else None,
    )
    run.extra = {"text": text}
    turn_started = datetime.now(timezone.utc)

    async def finish_turn() -> dict:
        token = run_cancel.current.set(run)
        watcher = asyncio.create_task(run_cancel.watch(run))
        try:
            return await _run_turn()
        except (asyncio.CancelledError, run_cancel.TurnCancelled):
            if not run.cancelled:
                raise
            task = asyncio.current_task()
            if task is not None and hasattr(task, "uncancel"):
                task.uncancel()
            return await _store_stopped(db, user, sess, working_message, progress, run, turn_started, public_uploads)
        finally:
            watcher.cancel()
            run_cancel.unregister(request_id, run)
            run_cancel.current.reset(token)
            if run.cancelled:
                await run_cancel.clear_remote(run)

    async def _run_turn() -> dict:
        try:
            result = await run_deep_agent(
                db,
                user,
                sess.id,
                request_id,
                history,
                agent_text,
                confirms,
                workspace_agent=workspace_agent,
                group=group,
                teammates=teammates,
                mentions=mentions,
                progress=progress,
            )
            # #region agent log
            dbg(
                "C",
                "chat/routes.py:chat",
                "agent returned",
                {
                    "ms": int((time.time() - t0) * 1000),
                    "n_tools": len(result.get("used_tools") or []),
                    "used_tools": (result.get("used_tools") or [])[:16],
                },
            )
            # #endregion
        except Exception as exc:  # noqa: BLE001
            if run.cancelled or isinstance(exc, run_cancel.TurnCancelled):
                raise run_cancel.TurnCancelled("stopped") from exc
            log.exception("chat agent failed")
            # #region agent log
            dbg("C", "chat/routes.py:chat", "agent exception", {"error": str(exc)[:200], "ms": int((time.time() - t0) * 1000)})
            # #endregion
            working_message.content = "I couldn't finish that request. You can retry or continue in this chat."
            working_message.meta = {**(working_message.meta or {}), "in_progress": False, "failed": True}
            try:
                await db.commit()
            finally:
                if progress is not None:
                    await progress.finish()
            raise HTTPException(502, str(exc)[:400]) from exc
        if run.cancelled:
            raise run_cancel.TurnCancelled("stopped")
        run_cancel.unregister(request_id, run)
        try:
            if result.get("source"):
                working["last_source"] = result["source"]
            sess.state = {
                **working,
                "last_tools": result.get("used_tools") or [],
            }
            meta = {
                "request_id": request_id,
                "route": result.get("route"),
                "used_tools": result.get("used_tools"),
                "skills_used": result.get("skills_used") or [],
                "task_state": result.get("task_state"),
                "pending_confirms": result.get("pending_confirms"),
                "attachments": result.get("attachments"),
                "source": result.get("source"),
                "mail_threads": result.get("mail_threads") or [],
                "browser": result.get("browser") or [],
                "computer": result.get("computer") or {},
                "connect_cards": result.get("connect_cards") or [],
                "speaker": result.get("speaker"),
                "agent_id": (result.get("speaker") or {}).get("id") if result.get("speaker") else (str(workspace_agent.id) if workspace_agent else None),
                "agent_name": (result.get("speaker") or {}).get("name"),
                "agent_color": (result.get("speaker") or {}).get("color"),
                "agent_avatar": (result.get("speaker") or {}).get("avatar"),
                "group_id": result.get("group_id"),
                "steps": [s.get("text") for s in (progress.steps if progress else []) if s.get("kind") != "done"][:40],
                "jobs": result.get("jobs_started") or [],
                "job": (result.get("jobs_started") or [None])[0],
            }
            if isinstance(result.get("question"), dict) and result["question"].get("options"):
                # ask_user in a chat turn: the web shows the options as buttons that send the option text.
                meta["choices"] = {"question": result["question"].get("question"), "options": result["question"]["options"][:5]}
            await _store(
                db,
                sess,
                user.id,
                request_id,
                text,
                result["text"],
                meta,
                user_meta={"uploads": public_uploads} if public_uploads else None,
                thread=result.get("thread"),
                working_message=working_message,
            )
            return {"session_id": str(sess.id), **result, "request_id": request_id, "uploads": public_uploads}
        except Exception as exc:  # noqa: BLE001
            log.exception("chat store failed")
            raise HTTPException(500, str(exc)[:400]) from exc
        finally:
            # "Done" only after the reply is stored, so a client that lost its POST and polls progress
            # finds the finished message (not the working placeholder) when it reloads the session.
            if progress is not None:
                await progress.finish()

    # Long turns (computer use, research) can run for minutes. iOS Safari fails a fetch with "Load failed"
    # when no byte arrives for ~60s, and proxies drop idle requests, so after a short wait the reply is
    # streamed: JSON-insignificant spaces as heartbeats, then the same JSON body. The turn runs in its own
    # task, so a dropped client never cancels it; the web app then recovers the stored reply.
    task = asyncio.create_task(finish_turn())
    run.task = task
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=HEARTBEAT_AFTER_S)
    except asyncio.TimeoutError:
        pass
    return StreamingResponse(
        _heartbeat_body(task),
        media_type="application/json",
        headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"},
    )


HEARTBEAT_AFTER_S = 8.0
HEARTBEAT_EVERY_S = 10.0


async def _heartbeat_body(task: "asyncio.Task[dict]"):
    """Yield a space every few seconds while ``task`` runs, then its JSON result (or a JSON error)."""
    try:
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_EVERY_S)
            if done:
                break
            yield b" "
    finally:
        if not task.done():
            # The client went away. Keep this response (and its DB session) open until the turn has stored
            # its reply, so the user finds it when they come back.
            with anyio.CancelScope(shield=True):
                await asyncio.wait({task})
    try:
        data: dict = task.result()
    except HTTPException as exc:
        data = {"detail": exc.detail, "status_code": exc.status_code, "stream_error": True}
    except Exception as exc:  # noqa: BLE001
        data = {"detail": str(exc)[:400] or "Chat failed", "status_code": 500, "stream_error": True}
    yield json.dumps(jsonable_encoder(data)).encode("utf-8")


class SummarizeIn(BaseModel):
    message_id: str
    attachment_id: str
    connector_slug: str


@router.get("/thread")
async def chat_thread(
    slug: str = "",
    thread_id: str = "",
    message_id: str = "",
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.inbox import service as inbox_service

    try:
        return await inbox_service.open_full_thread(db, user, slug=slug, thread_id=thread_id, message_id=message_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, str(exc)[:300]) from exc


@router.post("/attachments/summarize")
async def summarize_attachment(body: SummarizeIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    row = (
        await db.execute(
            select(Connector).where(Connector.user_id == user.id, Connector.slug == body.connector_slug, Connector.enabled.is_(True))
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Connector not found")
    try:
        result = await summarize_one(user, RuntimeConnector(row), body.message_id, body.attachment_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, str(exc)[:300]) from exc
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


async def _resolve_roster(db, user, agent_id, group_id):
    chief = await agents_service.ensure_default_agent(db, user)
    if group_id:
        group, teammates = await agents_service.get_group(db, user, group_id)
        from app.agents.collab import pick_owner

        lead = pick_owner(teammates, group) or (teammates[0] if teammates else chief)
        return lead, group, teammates
    if agent_id:
        return await agents_service.get_agent(db, user, agent_id), None, []
    return chief, None, []


async def _maybe_start_job(db, user, sess, agent, group, confirms, text, agent_text, history, request_id):
    """Coding agents: a code-change request becomes a background job with a live timeline.

    That keeps long plan/build/test/push work off the chat request (and its ~5 minute proxy timeout)."""
    from app.agent.coding import continues_job, first_step, is_coding_agent, looks_like_coding_task
    from app.jobs import service as jobs
    from app.jobs.worker import wake

    if group is not None or confirms or not is_coding_agent(agent):
        return None
    latest = await jobs.latest_chat_job(db, user.id, sess.id, agent.id)
    if latest is not None and continues_job(text, latest.status, str((latest.meta or {}).get("repo") or "")):
        return await _continue_job(db, user, sess, agent, latest, text, request_id)
    if not looks_like_coding_task(text):
        return None
    context = "\n".join(f"{m['role']}: {str(m['content'])[:600]}" for m in history[-6:])
    prompt = agent_text + (f"\n\nRecent conversation for context:\n{context}" if context else "")
    try:
        job = await jobs.create_job(db, user, agent=agent, prompt=prompt, title=text, role="planner", kind="chat", session_id=sess.id)
    except jobs.JobError:
        return None
    info = jobs.serialize_job(job)
    reply = (
        f"On it: {first_step(text)}. Follow the live timeline below; pushes, merges and deploys wait for your approval there."
    )
    speaker = {"id": str(agent.id), "name": agent.name, "color": getattr(agent, "color", "") or "", "title": getattr(agent, "title", "") or ""}
    meta = {"request_id": request_id, "route": "job", "job": info, "jobs": [info], "speaker": speaker, "agent_id": str(agent.id), "agent_name": agent.name}
    await _store(db, sess, user.id, request_id, text, reply, meta)
    wake()
    return {
        "session_id": str(sess.id),
        "text": reply,
        "route": "job",
        "job": info,
        "jobs_started": [info],
        "pending_confirms": [],
        "used_tools": [],
        "speaker": speaker,
        "request_id": request_id,
        "uploads": [],
    }


async def _continue_job(db, user, sess, agent, job, text, request_id):
    """Follow-ups ("also fix X", "merge it", an answer to its question) continue the same job and branch."""
    from app.jobs import service as jobs
    from app.jobs.worker import wake

    was = job.status
    pasted_code = jobs.is_code_answer(job, text)
    try:
        events = jobs.followup(job, text)
    except jobs.JobError:
        return None
    for ev in events:
        db.add(ev)
    info = jobs.serialize_job(job)
    if was == "waiting_input":
        reply = "Got it, continuing the job with your answer."
    elif was in {"done", "failed"}:
        reply = "On it: continuing the same job, repo and branch. Follow the timeline below."
    else:
        reply = "Noted. I'll pick that up in the running job as soon as the current step finishes."
    speaker = {"id": str(agent.id), "name": agent.name, "color": getattr(agent, "color", "") or "", "title": getattr(agent, "title", "") or ""}
    meta = {"request_id": request_id, "route": "job", "jobs": [info], "speaker": speaker, "agent_id": str(agent.id), "agent_name": agent.name, "followup": True}
    if was in {"done", "failed"}:
        # The earlier timeline stopped polling; show a fresh one that starts after what was already shown.
        meta["job"] = info
        meta["job_after"] = await _last_event_id(db, job)
    else:
        meta["job_ref"] = info  # its live timeline is already on screen
    if pasted_code:
        reply = "Got it, finishing the sign-in with that code and continuing the job."
    await _store(db, sess, user.id, request_id, "[authorization code sent]" if pasted_code else text, reply, meta)
    wake()
    return {
        "session_id": str(sess.id),
        "text": reply,
        "route": "job",
        "job": info,
        "jobs_started": [info],
        "pending_confirms": [],
        "used_tools": [],
        "speaker": speaker,
        "request_id": request_id,
        "uploads": [],
    }


async def _last_event_id(db, job) -> int:
    try:
        from sqlalchemy import func, select

        from app.models import JobEvent

        return int((await db.execute(select(func.max(JobEvent.id)).where(JobEvent.root_id == (job.root_id or job.id)))).scalar() or 0)
    except Exception:  # noqa: BLE001
        return 0


async def _start_turn(db, sess, user_id, request_id, user_text, user_meta=None, progress_id=None, agent=None, group=None):
    """Commit a user turn and visible working marker before a long agent run starts."""
    from app.models import ChatMessage

    agent_id = getattr(agent, "id", None)
    agent_name = getattr(agent, "name", None)
    agent_avatar = getattr(agent, "avatar", "") or ""
    shared_meta = {
        "agent_id": str(agent_id) if agent_id else None,
        "agent_name": agent_name,
        "agent_avatar": agent_avatar,
        "group_id": str(getattr(group, "id", "")) if group is not None else None,
    }
    db.add(ChatMessage(session_id=sess.id, user_id=user_id, role="user", content=user_text, meta=user_meta or {}, request_id=request_id))
    working = ChatMessage(
        session_id=sess.id,
        user_id=user_id,
        role="assistant",
        content=f"{agent_name or 'Your agent'} is working on this…",
        meta={**shared_meta, "request_id": request_id, "in_progress": True, "progress_id": progress_id},
        request_id=request_id,
    )
    db.add(working)
    if not sess.title or sess.title == "New chat":
        sess.title = user_text[:80]
    sess.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return working


async def _store(db, sess, user_id, request_id, user_text, assistant_text, meta, user_meta=None, thread=None, working_message=None) -> None:
    if working_message is None:
        db.add(ChatMessage(session_id=sess.id, user_id=user_id, role="user", content=user_text, meta=user_meta or {}, request_id=request_id))
        db.add(ChatMessage(session_id=sess.id, user_id=user_id, role="assistant", content=assistant_text, meta=meta, request_id=request_id))
    else:
        working_message.content = assistant_text
        working_message.meta = {**(working_message.meta or {}), **meta, "in_progress": False}
    for item in thread or []:
        if not isinstance(item, dict):
            continue
        text = (item.get("text") or "").strip()
        if not text:
            continue
        db.add(
            ChatMessage(
                session_id=sess.id,
                user_id=user_id,
                role="assistant",
                content=text,
                meta={
                    "agent_id": item.get("agent_id"),
                    "agent_name": item.get("agent_name"),
                    "agent_color": item.get("agent_color") or "",
                    "agent_avatar": item.get("agent_avatar") or "",
                    "peer": True,
                    "used_tools": item.get("used_tools") or [],
                    "computer": item.get("computer") or {},
                    "attachments": item.get("attachments") or [],
                    "request_id": request_id,
                },
                request_id=request_id,
            )
        )
    if not sess.title or sess.title == "New chat":
        sess.title = user_text[:80]
    sess.updated_at = datetime.now(timezone.utc)
    await db.commit()


# ---------------------------------------------------------------- Stop / Continue / interrupt (docs/LLD.md §7.9)

STOP_WAIT_S = 6.0
QUEUE_WAIT_S = 900.0


async def _wait_done(runs: list, timeout: float) -> None:
    tasks = {r.task for r in runs if r.task is not None and not r.task.done()}
    if tasks:
        await asyncio.wait(tasks, timeout=timeout)


async def _settle_running(user, sess, mode: str | None) -> dict | None:
    """A new message while a turn runs in this chat: interrupt it (default) or wait for it ("queue").

    Returns what the interrupted turn had done, for the new turn's context."""
    runs = [r for r in run_cancel.find(user.id, session_id=sess.id, kind="turn") if not r.cancelled]
    if not runs:
        return None
    interrupt = mode == "interrupt" or (mode != "queue" and get_settings().chat_interrupt_on_new_message)
    if not interrupt:
        await _wait_done(runs, QUEUE_WAIT_S)
        return None
    run_cancel.cancel_runs(runs, "interrupted")
    await _wait_done(runs, STOP_WAIT_S)
    last = runs[-1]
    stopped = (getattr(last, "extra", None) or {}).get("stopped") or {}
    return {"text": (getattr(last, "extra", None) or {}).get("text", ""), "partial": stopped.get("partial", ""), "steps": stopped.get("steps", [])}


async def _store_stopped(db, user, sess, working_message, progress, run, since, uploads) -> dict:
    """Persist the "Stopped" marker (keeping the partial reply), cancel jobs the turn started, clear approvals."""
    from app.jobs import service as jobs

    partial = (getattr(progress, "draft_text", "") or "") if progress is not None else ""
    steps = [st.get("text") for st in (progress.steps if progress is not None else []) if st.get("kind") != "done" and st.get("text")][:40]
    content = run_cancel.stopped_text(partial, steps)
    run.extra = {**(getattr(run, "extra", None) or {}), "stopped": {"partial": partial, "steps": steps}}
    try:
        with anyio.CancelScope(shield=True):
            await db.rollback()
            await db.refresh(working_message)
            working_message.content = content
            working_message.meta = {
                **(working_message.meta or {}),
                "in_progress": False,
                "stopped": True,
                "can_continue": True,
                "stopped_reason": run.reason or "stopped",
                "steps": steps,
                "pending_confirms": [],
            }
            sess.updated_at = datetime.now(timezone.utc)
            await jobs.stop_chat_jobs(db, user.id, sess.id, since=since)
            await db.commit()
    except Exception:  # noqa: BLE001
        log.exception("could not store the stopped marker")
    finally:
        if progress is not None:
            await progress.finish()
    return {
        "session_id": str(sess.id),
        "text": content,
        "stopped": True,
        "can_continue": True,
        "route": "stopped",
        "request_id": run.request_id,
        "pending_confirms": [],
        "used_tools": [],
        "steps": steps,
        "uploads": uploads,
    }


async def _resume_context(db, sess, request_id: str) -> dict | None:
    rows = (
        await db.execute(
            select(ChatMessage).where(ChatMessage.session_id == sess.id, ChatMessage.request_id == str(request_id)[:64]).order_by(ChatMessage.id)
        )
    ).scalars().all()
    asked = next((m for m in rows if m.role == "user"), None)
    stopped = next((m for m in rows if m.role == "assistant" and (m.meta or {}).get("stopped")), None)
    if asked is None or stopped is None:
        return None
    marker = "⏹ Stopped."
    partial = (stopped.content or "").split(marker)[0].strip()
    if partial.startswith("Progress before stopping:"):
        partial = ""
    return {"text": asked.content or "", "partial": partial, "steps": list((stopped.meta or {}).get("steps") or [])}


@router.post("/cancel")
async def cancel_turn(body: CancelIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Stop the running turn (and the jobs it started) in a chat. Partial output is kept; Continue resumes it."""
    if not (body.request_id or body.progress_id or body.session_id):
        raise HTTPException(400, "Say which turn to stop")
    if body.progress_id and not valid_progress_id(body.progress_id):
        raise HTTPException(400, "Bad progress id")
    session_id = body.session_id
    if session_id is None and body.progress_id:
        found = await read_progress_session(user.id, body.progress_id)
        session_id = uuid.UUID(found) if found else None
    if session_id is not None:
        sess = await db.get(ChatSession, session_id)
        if sess is None or sess.user_id != user.id:
            raise HTTPException(404, "Not found")
    runs = run_cancel.find(
        user.id, request_id=body.request_id, progress_id=body.progress_id, session_id=None if (body.request_id or body.progress_id) else session_id,
        agent_id=body.agent_id, kind="turn",
    )
    run_cancel.cancel_runs(runs, "stopped")
    remote = False
    if not runs:
        remote = await run_cancel.request_remote(user.id, [body.request_id, body.progress_id, str(session_id) if session_id else None])
    await _wait_done(runs, STOP_WAIT_S)
    from app.jobs import service as jobs

    stopped_jobs = await jobs.stop_chat_jobs(db, user.id, session_id, agent_id=body.agent_id) if session_id is not None else []
    recovered = 0
    if not runs and session_id is not None:
        # Nothing is running here: another process may own the turn (it sees the flag within seconds), or the turn
        # died with its process. Either way the chat must not stay stuck on "working".
        await asyncio.sleep(run_cancel.WATCH_EVERY_S * 2 if remote else 0)
        recovered = await _unstick(db, user, session_id, body.request_id, body.progress_id)
    await db.commit()
    return {
        "ok": True,
        "stopped": len(runs) + recovered,
        "jobs": [jobs.serialize_job(j) for j in stopped_jobs],
        "session_id": str(session_id) if session_id else None,
    }


@router.post("/turns/{request_id}/cancel")
async def cancel_turn_by_id(request_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await cancel_turn(CancelIn(request_id=request_id), user, db)


async def _unstick(db, user, session_id, request_id=None, progress_id=None) -> int:
    rows = (
        await db.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id, ChatMessage.user_id == user.id, ChatMessage.role == "assistant")
            .order_by(ChatMessage.id.desc())
            .limit(20)
        )
    ).scalars().all()
    n = 0
    for m in rows:
        meta = m.meta or {}
        if not meta.get("in_progress"):
            continue
        if request_id and m.request_id != request_id:
            continue
        if progress_id and meta.get("progress_id") != progress_id:
            continue
        await db.refresh(m)
        if not (m.meta or {}).get("in_progress"):
            continue  # finished meanwhile
        m.content = run_cancel.stopped_text("", list(meta.get("steps") or []))
        m.meta = {**meta, "in_progress": False, "stopped": True, "can_continue": True, "pending_confirms": []}
        n += 1
    return n
