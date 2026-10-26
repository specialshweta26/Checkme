"""Run one turn of a background job and record it on the timeline."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from app.jobs import service
from app.models import AgentJob, ChatSession, User, WorkspaceAgent
from connector_core.text import clip

log = logging.getLogger(__name__)

ROLE_BRIEF = {
    "planner": (
        "You are the planner for this background coding job. Acknowledge, then write_todos with a short plan. Check "
        "coding_workspace_status and explore the repo remotely with the GitHub App read tools. Small, clear changes you may "
        "build yourself; otherwise hand the build to a coder with delegate(role='coder', ...) giving the repo, the branch "
        "(the user's convention or agent/<short-name>) and acceptance criteria, and end your turn. When the coder reports a "
        "PR, delegate(role='reviewer', ...) naming the repo and PR number. If the reviewer asks for changes, delegate one fix "
        "round to the coder, then review again. Do not merge by default: the job ends by asking the user 'Merge it?'. Merge "
        "only after an explicit yes (or when the task says to merge): check_merge first, then github-app "
        "merge_pull_request, whose approval card the user must approve; never say it is merged until the result says so. "
        "Finish with the PR link, what changed, the checks, the review verdict and any caveats."
    ),
    "coder": (
        "You are the coder for this background job. write_todos first. git_checkout the branch (agent/<short-name> unless "
        "you were given an existing PR branch), ensure_dev_toolchain if needed, repo_overview before editing, then "
        "find_symbol / find_references / read_file_range, edit with sandbox_shell, run_checks until they pass (up to 3 "
        "attempts), git_diff, git_commit, then push_branch with a body of ## Summary, ## Changes, ## Tests, ## Caveats (it "
        "waits for the user's approval; pr_number updates an existing PR). Merge only if the task explicitly says the user "
        "said yes. Finish with the PR link, what changed and the check results."
    ),
    "reviewer": (
        "You are the reviewer for this background job. Read the pull request (get_pull_request, compare_refs, "
        "get_pr_comments), check out its branch and run_checks when useful, then post a structured review with "
        "review_pull_request: summary, findings by severity with path and line, and a verdict (APPROVE only when nothing "
        "blocks, otherwise REQUEST_CHANGES or COMMENT). Never push and never merge. Finish with the verdict and the "
        "changes needed."
    ),
}


def job_query(job: AgentJob) -> str:
    query = service.next_query(job)
    if (job.meta or {}).get("history"):
        return query
    brief = ROLE_BRIEF.get(job.role, "")
    head = f"[Background job · {job.role} · depth {job.depth}] "
    if job.kind == "scheduled":
        head += "This is a scheduled run. "
    return f"{head}{brief}\n\nTask:\n{query}"


class JobProgress:
    """Progress reporter for jobs: status lines become JobEvents (and keep the heartbeat fresh)."""

    MIN_GAP_S = 0.0

    def __init__(self, job: AgentJob, session_factory: Callable, *, agent_name: str = "", prefix: str = ""):
        self.job = job
        self.session_factory = session_factory
        self.agent_name = agent_name
        self.prefix = prefix
        self.steps: list[dict] = []
        self._parent: JobProgress | None = None
        self.enabled = True
        self.progress_id = None

    def child(self, agent_name: str) -> "JobProgress":
        kid = JobProgress(self.job, self.session_factory, agent_name=agent_name, prefix=f"{agent_name}: ")
        kid._parent = self
        return kid

    async def _write(self, kind: str, text: str, data: dict | None = None) -> None:
        try:
            async with self.session_factory() as db:
                db.add(service.event(self.job, kind, text, data, agent_name=self.agent_name))
                row = await db.get(AgentJob, self.job.id)
                if row is not None:
                    row.heartbeat_at = datetime.now(timezone.utc)
                await db.commit()
        except Exception as exc:  # noqa: BLE001
            log.debug("job event write failed: %s", exc)

    async def emit(self, text: str | None, kind: str = "step", tool: str = "") -> None:
        if not text:
            return
        if self._parent is not None:
            await self._parent.emit(self.prefix + text, kind, tool)
            return
        line = clip(self.prefix + text, 300)
        if self.steps and self.steps[-1]["text"] == line:
            return
        self.steps.append({"text": line, "kind": kind, "tool": tool, "at": round(time.time(), 2)})
        await self._write("plan" if tool == "write_todos" else "step", line, {"tool": tool, "step": kind} if tool else {"step": kind})

    async def record(self, kind: str, text: str, data: dict | None = None) -> None:
        if self._parent is not None:
            await self._parent.record(kind, text, data)
            return
        await self._write(kind, text, data)

    async def finish(self) -> None:  # the job status event covers "done"
        return None


async def run_job(job_id, session_factory: Callable | None = None, agent_impl: Callable | None = None) -> str:
    """Run one turn of a job. Returns the job's status afterwards."""
    if session_factory is None:
        from app.db import SessionLocal

        session_factory = SessionLocal
    if agent_impl is None:
        from app.agent.deep import run_deep_agent

        agent_impl = run_deep_agent
    async with session_factory() as db:
        job = await db.get(AgentJob, job_id)
        if job is None:
            return "missing"
        if job.status != "running":
            return job.status
        user = await db.get(User, job.user_id)
        if user is None:
            ev = service.fail(job, "User is missing.")
            db.add(ev)
            await db.commit()
            return job.status
        agent = await db.get(WorkspaceAgent, job.agent_id) if job.agent_id else None
        if agent is None:
            from app.agents.service import ensure_default_agent

            agent = await ensure_default_agent(db, user)
        if job.session_id is None:
            sess = ChatSession(
                user_id=user.id, title=f"Job · {job.title}"[:200], state={"job_id": str(job.id)}, agent_id=agent.id
            )
            db.add(sess)
            await db.flush()
            job.session_id = sess.id
        meta = job.meta or {}
        query = job_query(job)
        progress = JobProgress(job, session_factory, agent_name=agent.name)
        db.add(service.event(job, "status", f"{agent.name} is working ({job.role})", {"status": "running"}))
        await db.commit()
        try:
            result = await agent_impl(
                db,
                user,
                job.session_id,
                f"job-{str(job.id)[:8]}-{job.attempts or 0}",
                list(meta.get("history") or []),
                query,
                list(meta.get("confirmations") or []),
                auto_confirm=job.kind == "scheduled",
                workspace_agent=agent,
                progress=progress,
                job=job,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("job %s failed", job.id)
            await db.rollback()
            await db.refresh(job)
            if job.status == "cancelled":
                await db.commit()
                return job.status
            db.add(service.fail(job, str(exc)[:500]))
            await service.after_terminal(db, job)
            await db.commit()
            return job.status
        await db.refresh(job)  # picks up a cancel (and heartbeats) written by other sessions
        if job.status == "cancelled":
            await db.commit()
            return job.status
        for ev in service.apply_outcome(job, query, result):
            db.add(ev)
        if job.status in service.TERMINAL:
            await service.after_terminal(db, job)
        elif job.status == "waiting_input" and job.parent_id is None:
            await service.post_to_chat(db, job)
        elif job.status == "waiting_approval" and job.parent_id is None and job.agent_id:
            agent_row = await db.get(WorkspaceAgent, job.agent_id)
            if agent_row is not None:
                agent_row.unread = True
        await db.commit()
        return job.status
