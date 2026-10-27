"""Background agent jobs: creation, state transitions, timeline events and approvals.

A job is one agent run that happens outside the chat request (so it is not bound by the
~5 minute proxy timeout). Jobs form a tree: a planner can delegate to coder / reviewer
children (depth capped at MAX_DEPTH). The worker (app/jobs/worker.py) runs queued jobs;
app/jobs/runner.py turns one agent turn into events and the next status.

Status flow:
  queued -> running -> done | failed
                    -> waiting_approval  (an approval card is pending; approve/decline requeues)
                    -> waiting_children  (delegated; requeued when every child is finished)
                    -> waiting_input     (asked the user a question with option buttons, e.g. "Merge it?";
                                          the answer or any follow-up message requeues it)
  done | failed -> queued                (a follow-up in the same chat reopens the job: same repo/branch)
  any non-terminal -> cancelled
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.confirm import read_confirm
from app.models import AgentJob, ChatMessage, JobEvent, User, WorkspaceAgent
from connector_core.text import clip

log = logging.getLogger(__name__)

TERMINAL = {"done", "failed", "cancelled"}
ACTIVE = {"queued", "running", "waiting_approval", "waiting_children", "waiting_input"}
MERGE_OPTIONS = ("Yes, merge it", "No, leave it open")
MAX_OPTIONS = 5
ROLES = ("planner", "coder", "reviewer")
KINDS = ("chat", "delegate", "scheduled")
MAX_DEPTH = 3
MAX_CHILDREN = 4
HISTORY_TURNS = 16


class JobError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def serialize_job(job: AgentJob, agent_name: str = "") -> dict:
    meta = job.meta or {}
    return {
        "id": str(job.id),
        "root_id": str(job.root_id or job.id),
        "parent_id": str(job.parent_id) if job.parent_id else None,
        "agent_id": str(job.agent_id) if job.agent_id else None,
        "agent_name": agent_name or meta.get("agent_name") or "",
        "session_id": str(job.session_id) if job.session_id else None,
        "role": job.role,
        "kind": job.kind,
        "depth": job.depth or 1,
        "title": job.title,
        "status": job.status,
        "result": clip(job.result or "", 4000),
        "error": job.error or "",
        "pending": [
            {k: p.get(k) for k in ("token", "title", "preview", "kind")} for p in (meta.get("pending") or [])
        ],
        "question": meta.get("question") if job.status == "waiting_input" else None,
        "repo": meta.get("repo") or "",
        "prs": list(meta.get("prs") or [])[-5:],
        "created_at": _iso(job.created_at),
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
    }


def serialize_event(ev: JobEvent) -> dict:
    return {
        "id": ev.id,
        "job_id": str(ev.job_id),
        "agent_name": ev.agent_name or "",
        "kind": ev.kind,
        "text": ev.text or "",
        "data": ev.data or {},
        "at": _iso(ev.created_at),
    }


def event(job: AgentJob, kind: str, text: str, data: dict | None = None, agent_name: str = "") -> JobEvent:
    return JobEvent(
        job_id=job.id,
        root_id=job.root_id or job.id,
        user_id=job.user_id,
        agent_name=(agent_name or (job.meta or {}).get("agent_name") or "")[:80],
        kind=kind,
        text=clip(text or "", 4000),
        data=data or {},
        created_at=_now(),
    )


def new_job(
    user_id,
    *,
    agent: WorkspaceAgent | None,
    prompt: str,
    title: str = "",
    role: str = "coder",
    kind: str = "chat",
    session_id=None,
    parent: AgentJob | None = None,
    scheduled_task_id=None,
    history: list[dict] | None = None,
) -> AgentJob:
    """Build an AgentJob (not yet added to a session). Enforces depth and role limits."""
    if role not in ROLES:
        raise JobError(f"role must be one of {', '.join(ROLES)}")
    if kind not in KINDS:
        raise JobError(f"kind must be one of {', '.join(KINDS)}")
    prompt = (prompt or "").strip()
    if not prompt:
        raise JobError("A job needs a task description.")
    depth = (parent.depth or 1) + 1 if parent is not None else 1
    if depth > MAX_DEPTH:
        raise JobError(f"Delegation is capped at {MAX_DEPTH} levels; do this step yourself.")
    job_id = uuid.uuid4()
    return AgentJob(
        id=job_id,
        user_id=user_id,
        agent_id=getattr(agent, "id", None),
        session_id=session_id if session_id is not None else (parent.session_id if parent is not None else None),
        parent_id=parent.id if parent is not None else None,
        root_id=(parent.root_id or parent.id) if parent is not None else job_id,
        depth=depth,
        role=role,
        kind=kind,
        title=clip(title or prompt.splitlines()[0], 200),
        prompt=prompt,
        status="queued",
        result="",
        error="",
        meta={
            "agent_name": getattr(agent, "name", "") or "",
            "history": list(history or [])[-HISTORY_TURNS:],
            "confirmations": [],
            "pending": [],
        },
        attempts=0,
        scheduled_task_id=scheduled_task_id,
        created_at=_now(),
        updated_at=_now(),
    )


async def create_job(db: AsyncSession, user: User, **kwargs: Any) -> AgentJob:
    job = new_job(user.id, **kwargs)
    db.add(job)
    db.add(event(job, "status", f"Queued: {job.title}", {"status": "queued", "role": job.role}))
    return job


# ---- transitions (pure: they only mutate the job and return events to add) ----


def clean_question(raw: Any) -> dict | None:
    """{question, options[], waiting_for?} from ask_user, or None."""
    if not isinstance(raw, dict):
        return None
    q = clip(str(raw.get("question") or "").strip(), 400)
    opts = [clip(str(o).strip(), 80) for o in raw.get("options") or [] if str(o).strip()][:MAX_OPTIONS]
    if not q or len(opts) < 2:
        return None
    out = {"question": q, "options": opts}
    if raw.get("waiting_for"):
        out["waiting_for"] = str(raw["waiting_for"])[:40]
    if raw.get("pr"):
        out["pr"] = raw["pr"]
    return out


def _merge_prs(meta: dict, result: dict) -> None:
    prs = list(meta.get("prs") or [])
    for pr in result.get("prs") or []:
        if isinstance(pr, dict) and pr.get("url") and not any(p.get("url") == pr["url"] for p in prs):
            prs.append({k: pr.get(k) for k in ("url", "repo", "number", "branch", "title") if pr.get(k) is not None})
    for m in result.get("merged") or []:
        if not isinstance(m, dict):
            continue
        for p in prs:
            if str(p.get("repo", "")).lower() == str(m.get("repo", "")).lower() and int(p.get("number") or 0) == int(m.get("number") or -1):
                p["merged"] = True
                p["merge_sha"] = m.get("sha")
    if prs:
        meta["prs"] = prs[-10:]
    if result.get("repo") and not meta.get("repo"):
        meta["repo"] = result["repo"]


def merge_question(meta: dict) -> dict | None:
    """After a PR is opened, the job ends by asking "Merge it?" instead of merging by default."""
    open_prs = [p for p in meta.get("prs") or [] if not p.get("merged")]
    if not open_prs or meta.get("merge_asked"):
        return None
    pr = open_prs[-1]
    label = f"{pr.get('repo')}#{pr.get('number')}" if pr.get("number") else pr.get("url")
    return {"question": f"Merge it? ({label})", "options": list(MERGE_OPTIONS), "pr": pr}


def _take_followups(job: AgentJob, meta: dict) -> list[JobEvent] | None:
    ups = list(meta.get("followups") or [])
    if not ups:
        return None
    meta["followups"] = []
    meta["next_query"] = followup_query(ups)
    job.status = "queued"
    return [event(job, "status", "Picking up your follow-up", {"status": "queued"})]


def followup_query(texts: list[str]) -> str:
    joined = "\n".join(f"- {t}" for t in texts)
    return (
        "Follow-up from the user in the same chat (continue this job: same repo, same branch and PR unless they say "
        f"otherwise):\n{joined}\nAcknowledge in one line, update the todo list, then do it."
    )


def apply_outcome(job: AgentJob, query: str, result: dict) -> list[JobEvent]:
    """Fold one finished agent turn into the job. Returns the timeline events to store."""
    meta = dict(job.meta or {})
    text = (result.get("text") or "").strip() or "Done."
    history = list(meta.get("history") or [])
    history += [{"role": "user", "content": query}, {"role": "assistant", "content": text}]
    meta["history"] = history[-HISTORY_TURNS:]
    meta["confirmations"] = []
    meta.pop("next_query", None)
    meta.pop("question", None)
    _merge_prs(meta, result)
    pending = [p for p in (result.get("pending_confirms") or []) if isinstance(p, dict) and p.get("token")]
    children = [str(c.get("id") if isinstance(c, dict) else c) for c in (result.get("jobs_started") or [])]
    question = clean_question(result.get("question"))
    events: list[JobEvent] = []
    job.heartbeat_at = _now()
    if children:
        meta["children"] = list(dict.fromkeys([*(meta.get("children") or []), *children]))
    if pending:
        meta["pending"] = pending
        job.status = "waiting_approval"
        events.append(event(job, "message", text))
        for p in pending:
            events.append(
                event(
                    job,
                    "approval",
                    p.get("title") or "Approval needed",
                    {"token": p.get("token"), "title": p.get("title"), "preview": p.get("preview"), "kind": p.get("kind") or ""},
                )
            )
    elif children:
        meta["pending"] = []
        job.status = "waiting_children"
        events.append(event(job, "message", text))
        events.append(event(job, "status", f"Waiting for {len(children)} delegated job(s)", {"status": "waiting_children"}))
    else:
        meta["pending"] = []
        more = _take_followups(job, meta)
        if more is not None:
            events.append(event(job, "message", text))
            job.meta = meta
            return events + more
        if question is None and job.parent_id is None and job.kind == "chat":
            question = merge_question(meta)
        if question is not None:
            if question.get("pr"):
                meta["merge_asked"] = True
            meta["question"] = question
            job.status = "waiting_input"
            job.result = text
            events.append(event(job, "result", text, {"status": "waiting_input"}))
            events.append(event(job, "question", question["question"], question))
        else:
            job.status = "done"
            job.result = text
            job.finished_at = _now()
            events.append(event(job, "result", text, {"status": "done"}))
    job.meta = meta
    return events


def answer(job: AgentJob, text: str) -> list[JobEvent]:
    """The user picked an option (or typed a reply) for the job's question."""
    if job.status != "waiting_input":
        raise JobError("This job is not waiting for an answer.")
    meta = dict(job.meta or {})
    q = meta.pop("question", None) or {}
    text = clip((text or "").strip(), 2000)
    if not text:
        raise JobError("Empty answer.")
    extra = ""
    if q.get("pr"):
        pr = q["pr"]
        extra = (
            f" This was the merge question for {pr.get('repo')}#{pr.get('number')} ({pr.get('url')}). If the answer is yes, "
            "call check_merge, report any blockers (conflicts, failing checks) and, if it is mergeable, call "
            "merge_pull_request (merge commit unless they asked for squash/rebase); its approval card is the final "
            "confirmation. Then report the merge commit. If no, leave the PR open and finish."
        )
    meta["next_query"] = f"The user answered {q.get('question') or 'your question'!r}: {text!r}.{extra}"
    job.meta = meta
    job.status = "queued"
    job.finished_at = None
    return [event(job, "status", f"You answered: {clip(text, 120)}", {"status": "queued", "answer": text})]


def followup(job: AgentJob, text: str) -> list[JobEvent]:
    """A new chat message for an existing job: answer, queue, or reopen it (same repo and branch)."""
    text = clip((text or "").strip(), 4000)
    if job.status == "waiting_input":
        return answer(job, text)
    meta = dict(job.meta or {})
    if job.status in {"done", "failed"}:
        meta["next_query"] = followup_query([text])
        meta["pending"] = []
        job.meta = meta
        job.status = "queued"
        job.finished_at = None
        job.error = ""
        job.attempts = 0
        return [event(job, "status", f"Follow-up: {clip(text, 120)}", {"status": "queued", "reopened": True})]
    if job.status in TERMINAL:
        raise JobError("This job was cancelled.")
    meta["followups"] = [*(meta.get("followups") or []), text][-5:]
    job.meta = meta
    return [event(job, "status", f"Follow-up queued: {clip(text, 120)}", {"followup": True})]


def resume_waiting_for(job: AgentJob, what: str, note: str) -> list[JobEvent]:
    """Requeue a job that asked the user to connect something (e.g. GitHub through Composio) once it is connected."""
    q = (job.meta or {}).get("question") or {}
    if job.status != "waiting_input" or q.get("waiting_for") != what:
        return []
    meta = dict(job.meta or {})
    meta.pop("question", None)
    meta["next_query"] = note
    job.meta = meta
    job.status = "queued"
    return [event(job, "status", note.split(".")[0], {"status": "queued"})]


def fail(job: AgentJob, error: str) -> JobEvent:
    job.status = "failed"
    job.error = clip(error or "failed", 2000)
    job.finished_at = _now()
    return event(job, "error", f"Failed: {job.error}", {"status": "failed"})


def next_query(job: AgentJob) -> str:
    """What to send the agent on this run: the task on the first run, then the follow-up."""
    meta = job.meta or {}
    if meta.get("next_query"):
        return str(meta["next_query"])
    return job.prompt


def _find_pending(job: AgentJob, token: str) -> dict | None:
    for p in (job.meta or {}).get("pending") or []:
        if p.get("token") == token:
            return p
    return None


def approve(job: AgentJob, token: str) -> list[JobEvent]:
    """User approved one pending card: requeue the job with that confirmation."""
    if job.status != "waiting_approval":
        raise JobError("This job is not waiting for approval.")
    card = _find_pending(job, token)
    if card is None:
        raise JobError("That approval is not pending on this job.")
    data = read_confirm(token)
    if not data:
        raise JobError("That approval expired. Ask the agent to prepare it again.")
    meta = dict(job.meta or {})
    meta["confirmations"] = [data["payload"]]
    meta["pending"] = []
    meta["next_query"] = (
        f"The user approved: {card.get('title') or 'the action'}. Its result is above. "
        "Continue the task and finish with a short summary (include any PR link)."
    )
    job.meta = meta
    job.status = "queued"
    return [event(job, "status", f"Approved: {card.get('title') or 'action'}", {"status": "queued", "approved": True})]


def decline(job: AgentJob, token: str) -> list[JobEvent]:
    if job.status != "waiting_approval":
        raise JobError("This job is not waiting for approval.")
    card = _find_pending(job, token)
    if card is None:
        raise JobError("That approval is not pending on this job.")
    meta = dict(job.meta or {})
    meta["confirmations"] = []
    meta["pending"] = [p for p in meta.get("pending") or [] if p.get("token") != token]
    if meta["pending"]:
        job.meta = meta
        return [event(job, "status", f"Declined: {card.get('title') or 'action'}", {"declined": True})]
    meta["next_query"] = (
        f"The user declined: {card.get('title') or 'the action'}. Do not retry it. "
        "Wrap up with a short summary of what was done and what is left."
    )
    job.meta = meta
    job.status = "queued"
    return [event(job, "status", f"Declined: {card.get('title') or 'action'}", {"status": "queued", "declined": True})]


def cancel(job: AgentJob) -> list[JobEvent]:
    if job.status in TERMINAL:
        return []
    job.status = "cancelled"
    job.finished_at = _now()
    meta = dict(job.meta or {})
    meta["pending"] = []
    job.meta = meta
    return [event(job, "status", "Cancelled", {"status": "cancelled"})]


def children_summary(children: list[AgentJob]) -> str:
    lines = []
    for kid in children:
        name = (kid.meta or {}).get("agent_name") or kid.role
        body = kid.result if kid.status == "done" else (kid.error or kid.status)
        lines.append(f"- {kid.role} ({name}) [{kid.status}] {kid.title}: {clip(body or '', 1500)}")
    return "\n".join(lines)


def resume_parent(parent: AgentJob, children: list[AgentJob]) -> list[JobEvent]:
    """Requeue a waiting parent once all of its children are finished."""
    if parent.status != "waiting_children":
        return []
    want = set((parent.meta or {}).get("children") or [])
    mine = [c for c in children if str(c.id) in want] or children
    if any(c.status not in TERMINAL for c in mine):
        return []
    meta = dict(parent.meta or {})
    meta["next_query"] = (
        "Your delegated jobs finished:\n" + children_summary(mine) + "\n\n"
        "Continue: delegate the next step if needed, or finish with a short summary for the user "
        "(PR links, review notes, and anything waiting for their approval such as the merge)."
    )
    meta["children"] = []
    parent.meta = meta
    parent.status = "queued"
    return [event(parent, "status", "Delegated work finished; continuing", {"status": "queued"})]


# ---- db helpers ----


async def get_job(db: AsyncSession, user: User, job_id) -> AgentJob:
    try:
        jid = uuid.UUID(str(job_id))
    except ValueError as exc:
        raise LookupError("Unknown job") from exc
    job = await db.get(AgentJob, jid)
    if job is None or job.user_id != user.id:
        raise LookupError("Unknown job")
    return job


async def tree(db: AsyncSession, user: User, root_id, after: int = 0) -> dict:
    jobs = (
        await db.execute(
            select(AgentJob).where(AgentJob.user_id == user.id, AgentJob.root_id == root_id).order_by(AgentJob.created_at)
        )
    ).scalars().all()
    events = (
        await db.execute(
            select(JobEvent)
            .where(JobEvent.user_id == user.id, JobEvent.root_id == root_id, JobEvent.id > int(after or 0))
            .order_by(JobEvent.id)
            .limit(300)
        )
    ).scalars().all()
    root = next((j for j in jobs if j.id == root_id), None)
    return {
        "root": serialize_job(root) if root else None,
        "jobs": [serialize_job(j) for j in jobs],
        "events": [serialize_event(e) for e in events],
        "done": bool(jobs) and all(j.status in TERMINAL for j in jobs),
        "waiting": [serialize_job(j) for j in jobs if j.status in {"waiting_approval", "waiting_input"}],
    }


async def after_terminal(db: AsyncSession, job: AgentJob) -> None:
    """Finished job: wake the parent, or post the result to the chat for a root job."""
    if job.parent_id:
        parent = await db.get(AgentJob, job.parent_id)
        if parent is None:
            return
        kids = (await db.execute(select(AgentJob).where(AgentJob.parent_id == parent.id))).scalars().all()
        for ev in resume_parent(parent, list(kids)):
            db.add(ev)
        return
    await post_to_chat(db, job)


async def post_to_chat(db: AsyncSession, job: AgentJob, text: str | None = None) -> None:
    """The job's result (or its question, with option buttons) as one assistant message in the chat."""
    if job.session_id is None:
        return
    body = text or (job.result if job.status in {"done", "waiting_input"} else f"The background job stopped: {job.error or job.status}.")
    meta: dict[str, Any] = {"job": serialize_job(job), "job_result": True, "agent_id": str(job.agent_id) if job.agent_id else None}
    q = (job.meta or {}).get("question") if job.status == "waiting_input" else None
    if q:
        meta["choices"] = {"job_id": str(job.id), "question": q.get("question"), "options": list(q.get("options") or [])}
        if q.get("question") and q["question"] not in (body or ""):
            body = f"{body}\n\n{q['question']}" if body else q["question"]
    db.add(
        ChatMessage(
            session_id=job.session_id,
            user_id=job.user_id,
            role="assistant",
            content=body or "Done.",
            meta=meta,
            request_id=f"job-{str(job.id)[:8]}-{len((job.meta or {}).get('history') or [])}",
        )
    )
    if job.agent_id:
        agent = await db.get(WorkspaceAgent, job.agent_id)
        if agent is not None:
            agent.unread = True


async def latest_chat_job(db: AsyncSession, user_id, session_id, agent_id, within_s: int = 12 * 3600) -> AgentJob | None:
    """The most recent root chat job of this agent in this chat that a follow-up can continue."""
    from datetime import timedelta

    if session_id is None or agent_id is None:
        return None
    job = (
        await db.execute(
            select(AgentJob)
            .where(
                AgentJob.user_id == user_id,
                AgentJob.session_id == session_id,
                AgentJob.agent_id == agent_id,
                AgentJob.parent_id.is_(None),
            )
            .order_by(AgentJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if job is None or job.status == "cancelled":
        return None
    if job.status in {"done", "failed"}:
        ts = job.finished_at or job.updated_at or job.created_at
        if ts is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts is not None and _now() - ts > timedelta(seconds=within_s):
            return None
    return job


async def user_has_running(db: AsyncSession, user_id) -> bool:
    row = (
        await db.execute(select(AgentJob.id).where(AgentJob.user_id == user_id, AgentJob.status == "running").limit(1))
    ).first()
    return row is not None
