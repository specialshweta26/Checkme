"""Plan cards (docs/PLANS.md): a long task shows its plan and waits for the user before it runs.

When an agent moves work to the background from a chat (``start_background_task`` / ``start_coding_job``) it passes
the steps it means to take, the tools it expects to use and the actions that will need the user's approval. The job is
created as ``planned``: the worker does not pick it up. The chat shows the plan with a likely cost; the user can edit the
steps, then start it (or say "go ahead"), or cancel it. On start the approved steps become the job's checklist and are
written into its task, with the instruction to re-plan, and say why, if a step fails.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentJob

MAX_STEPS = 12
MAX_STEP_CHARS = 200
MAX_ITEMS = 8
ESTIMATE_DAYS = 14


_MARKER = re.compile(r"^(?:[-•*]|\d{1,2}[.)])\s+")


class PlanError(ValueError):
    pass


def _clean_list(items: Any, cap: int, chars: int) -> list[str]:
    if isinstance(items, str):
        items = items.splitlines()
    out: list[str] = []
    for item in list(items or []):
        text = _MARKER.sub("", " ".join(str(item or "").split())).strip()
        if text and text not in out:
            out.append(text[:chars])
        if len(out) >= cap:
            break
    return out


def clean_steps(steps: Any) -> list[str]:
    return _clean_list(steps, MAX_STEPS, MAX_STEP_CHARS)


def clean_items(items: Any) -> list[str]:
    return _clean_list(items, MAX_ITEMS, 80)


async def estimate(db: AsyncSession, user_id) -> dict | None:
    """Likely cost of one background task: this user's background spend over the last two weeks divided by the number
    of background tasks they ran. None until there is history (or when the models aren't priced)."""
    from app.agent.meter import usage_rows

    since = date.today() - timedelta(days=ESTIMATE_DAYS)
    rows = [r for r in await usage_rows(db, user_id, since) if r["feature"] == "jobs"]
    priced = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
    if not priced:
        return None
    runs = (
        await db.execute(
            select(func.count()).select_from(AgentJob).where(
                AgentJob.user_id == user_id, AgentJob.parent_id.is_(None), AgentJob.started_at.is_not(None),
                AgentJob.created_at >= func.now() - timedelta(days=ESTIMATE_DAYS),
            )
        )
    ).scalar_one()  # fmt: skip
    if not runs:
        return None
    return {"usd": round(sum(priced) / runs, 3), "basis": int(runs), "days": ESTIMATE_DAYS}


def propose(job: AgentJob, steps: list[str], tools: list[str], approvals: list[str], cost: dict | None):
    """Park a new job as ``planned`` with its plan. Returns the timeline event to add."""
    from app.jobs.service import event

    if not steps:
        raise PlanError("A plan needs at least one step.")
    job.status = "planned"
    job.meta = {**(job.meta or {}), "plan": {"steps": steps, "tools": tools, "approvals": approvals, "estimate": cost, "state": "proposed"}}
    return event(job, "plan", "Here’s my plan. Edit it if you like, then start it.", {"proposed": True, "steps": steps})


def plan_text(steps: list[str]) -> str:
    lines = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
    return (
        "\n\nThe user approved this plan. Follow it in order and keep the checklist current with write_todos:\n"
        f"{lines}\n"
        "If a step fails or turns out to be wrong, re-plan the remaining steps with write_todos and say why in one line, "
        "rather than stopping or silently doing something else. Ask before anything the plan didn't cover that needs approval."
    )


def approve(job: AgentJob, steps: Any = None) -> list:
    """The user started the plan (possibly after editing the steps): queue the job to run against it."""
    from app.jobs.service import event

    if job.status != "planned":
        raise PlanError("This task isn't waiting for its plan to be approved.")
    meta = dict(job.meta or {})
    plan = dict(meta.get("plan") or {})
    final = clean_steps(steps) if steps is not None else list(plan.get("steps") or [])
    if not final:
        raise PlanError("Keep at least one step, or cancel the task.")
    edited = final != list(plan.get("steps") or [])
    plan.update(steps=final, state="approved", edited=edited)
    meta["plan"] = plan
    job.meta = meta
    job.prompt = (job.prompt or "") + plan_text(final)
    job.status = "queued"
    todos = [{"content": s, "status": "pending"} for s in final]
    return [
        event(job, "plan", "Plan approved" + (" with your edits" if edited else "") + ". Starting.", {"todos": todos, "approved": True, "edited": edited}),
    ]


def serialize(job: AgentJob) -> dict | None:
    plan = (job.meta or {}).get("plan")
    if not plan:
        return None
    return {k: plan.get(k) for k in ("steps", "tools", "approvals", "estimate", "state", "edited")}
