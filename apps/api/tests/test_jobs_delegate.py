"""Delegation (planner -> coder -> reviewer) and scheduled runs that need approval."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.agent.confirm import sign_confirm
from app.jobs import service


class _DB:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None

    async def commit(self):
        return None


def _ctx(job, db=None, agent=None):
    from app.agent.deep import RunContext

    user = SimpleNamespace(id=job.user_id, timezone="Asia/Kolkata")
    agent = agent or SimpleNamespace(id=job.agent_id or uuid.uuid4(), name="Coder", focus="coding", kind="custom")
    return RunContext(db=db if db is not None else _DB(), user=user, connectors={}, agent=agent, job=job)


def _root(role="planner"):
    return service.new_job(uuid.uuid4(), agent=SimpleNamespace(id=uuid.uuid4(), name="Coder"), prompt="Add a health check to a/b", role=role)


def test_delegate_tool_only_in_jobs_below_depth_cap():
    from app.agent.deep import harness_tools

    root = _root()
    names = {t.name for t in harness_tools(_ctx(root))}
    assert "delegate" in names and "start_coding_job" not in names
    kid = service.new_job(root.user_id, agent=None, prompt="x", kind="delegate", parent=root)
    grand = service.new_job(root.user_id, agent=None, prompt="x", kind="delegate", parent=kid)
    assert "delegate" in {t.name for t in harness_tools(_ctx(kid))}
    assert "delegate" not in {t.name for t in harness_tools(_ctx(grand))}  # depth 3 is the cap


async def test_delegate_job_creates_linked_child_and_caps_fanout():
    from app.agent.deep import delegate_job

    root = _root()
    db = _DB()
    ctx = _ctx(root, db)
    out = await delegate_job(ctx, "coder", "Build it on agent/health", title="Build health check")
    assert out["delegated"] and out["role"] == "coder"
    child = next(o for o in db.added if type(o).__name__ == "AgentJob")
    assert child.parent_id == root.id and child.root_id == root.id and child.depth == 2 and child.kind == "delegate"
    assert ctx.jobs_started[0]["id"] == str(child.id)
    assert "error" in await delegate_job(ctx, "boss", "x")
    for _ in range(service.MAX_CHILDREN - 1):
        assert (await delegate_job(ctx, "reviewer", "review"))["delegated"]
    assert "At most" in (await delegate_job(ctx, "reviewer", "one too many"))["error"]


async def test_delegate_requires_job_context():
    from app.agent.deep import RunContext, delegate_job

    ctx = RunContext(db=_DB(), user=SimpleNamespace(id=uuid.uuid4()), connectors={})
    assert "only works inside" in (await delegate_job(ctx, "coder", "x"))["error"]


def test_planner_brief_keeps_merge_manual():
    from app.jobs.runner import ROLE_BRIEF

    planner = ROLE_BRIEF["planner"]
    assert "approval card" in planner and "Merge it?" in planner and "explicit yes" in planner and "check_merge" in planner
    assert "never merge" in ROLE_BRIEF["reviewer"] and "review_pull_request" in ROLE_BRIEF["reviewer"]
    assert "explicitly says the user said yes" in ROLE_BRIEF["coder"] and "run_checks" in ROLE_BRIEF["coder"]


async def test_scheduled_run_with_git_card_is_held_for_approval():
    from app.tasks.service import hold_for_approval

    db = _DB()
    user = SimpleNamespace(id=uuid.uuid4())
    agent = SimpleNamespace(id=uuid.uuid4(), name="Coder", unread=False)
    task = SimpleNamespace(id=uuid.uuid4(), title="Nightly deps bump", instruction="Bump deps in a/b", prompt="")
    sess = SimpleNamespace(id=uuid.uuid4())
    assert await hold_for_approval(db, user, task, agent, sess, "run", "ok", []) is None
    token = sign_confirm("tool", {"slug": "coding", "tool": "push_branch", "args": {"repo": "a/b", "branch": "agent/deps"}}, ttl=3600)
    held = await hold_for_approval(db, user, task, agent, sess, "run", "Ready to push", [{"token": token, "title": "Push agent/deps"}])
    job = next(o for o in db.added if type(o).__name__ == "AgentJob")
    assert job.status == "waiting_approval" and job.kind == "scheduled" and job.scheduled_task_id == task.id
    assert held["job"]["pending"][0]["token"] == token and "Push agent/deps" in held["pending_titles"]
    assert agent.unread is True
    assert [e.kind for e in db.added if type(e).__name__ == "JobEvent"] == ["message", "approval"]


async def test_scheduled_runs_still_gate_git_writes(monkeypatch):
    """auto_confirm (scheduled / job runs) skips ordinary confirmations but never a git write."""
    from app.agent import deep

    calls = []

    async def impl(slug, tool, args):
        calls.append(tool)
        return {"ok": True}

    conn = SimpleNamespace(slug="github", name="GitHub", alias="", provider="mcp", kind="mcp", description="")
    ctx = deep.RunContext(db=None, user=SimpleNamespace(id=uuid.uuid4()), connectors={"github": conn}, auto_confirm=True, call_impl=impl)
    out = await deep.call_user_tool(ctx, "github", "merge_pull_request", {"repo": "a/b", "number": 3})
    assert out["status"] == "awaiting_user_confirmation" and calls == []
    assert ctx.pending and ctx.pending[0]["token"]
    # unattended cards live long enough for the user to see them
    from app.agent.confirm import read_confirm
    import time

    assert read_confirm(ctx.pending[0]["token"])["exp"] > time.time() + 3600
