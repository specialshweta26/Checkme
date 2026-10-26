"""Background jobs: pure state transitions, the job progress reporter, chat routing and git-tool events."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.agent.coding import looks_like_coding_task
from app.agent.confirm import sign_confirm
from app.jobs import service


def _agent(name="Coder"):
    return SimpleNamespace(id=uuid.uuid4(), name=name)


def _job(**kw):
    return service.new_job(uuid.uuid4(), agent=_agent(), prompt=kw.pop("prompt", "Fix the bug in a/b"), **kw)


def test_new_job_root_and_depth_cap():
    root = _job(role="planner")
    assert root.root_id == root.id and root.depth == 1 and root.status == "queued"
    child = service.new_job(root.user_id, agent=_agent("Reviewer"), prompt="review", role="reviewer", kind="delegate", parent=root)
    assert child.root_id == root.id and child.parent_id == root.id and child.depth == 2
    grand = service.new_job(root.user_id, agent=_agent(), prompt="x", kind="delegate", parent=child)
    assert grand.depth == 3
    with pytest.raises(service.JobError):
        service.new_job(root.user_id, agent=_agent(), prompt="x", kind="delegate", parent=grand)
    with pytest.raises(service.JobError):
        _job(role="boss")
    with pytest.raises(service.JobError):
        _job(prompt="   ")


def test_outcome_done_records_history_and_result():
    job = _job()
    job.status = "running"
    events = service.apply_outcome(job, "do it", {"text": "Opened PR https://github.com/a/b/pull/3"})
    assert job.status == "done" and "pull/3" in job.result and job.finished_at
    assert [e.kind for e in events] == ["result"]
    assert job.meta["history"][-2:] == [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "Opened PR https://github.com/a/b/pull/3"},
    ]


def test_outcome_pending_then_approve_requeues_with_confirmation():
    job = _job()
    job.status = "running"
    token = sign_confirm("tool", {"slug": "coding", "tool": "push_branch", "args": {"repo": "a/b", "branch": "agent/x"}}, ttl=3600)
    pending = [{"token": token, "title": "Push agent/x to a/b", "preview": {"repo": "a/b"}, "kind": "git_push"}]
    events = service.apply_outcome(job, "go", {"text": "Waiting for approval", "pending_confirms": pending})
    assert job.status == "waiting_approval"
    assert [e.kind for e in events] == ["message", "approval"]
    assert events[1].data["token"] == token
    assert service.serialize_job(job)["pending"][0]["title"].startswith("Push")
    with pytest.raises(service.JobError):
        service.approve(job, "bogus")
    service.approve(job, token)
    assert job.status == "queued"
    assert job.meta["confirmations"] == [{"slug": "coding", "tool": "push_branch", "args": {"repo": "a/b", "branch": "agent/x"}}]
    assert "approved" in service.next_query(job)
    with pytest.raises(service.JobError):
        service.approve(job, token)  # no longer waiting


def test_expired_approval_is_refused():
    job = _job()
    job.status = "running"
    token = sign_confirm("tool", {"slug": "coding", "tool": "push_branch", "args": {}}, ttl=-5)
    service.apply_outcome(job, "go", {"text": "x", "pending_confirms": [{"token": token, "title": "Push"}]})
    with pytest.raises(service.JobError, match="expired"):
        service.approve(job, token)


def test_decline_requeues_with_wrap_up():
    job = _job()
    job.status = "running"
    token = sign_confirm("tool", {"slug": "coding", "tool": "push_branch", "args": {}}, ttl=3600)
    service.apply_outcome(job, "go", {"text": "x", "pending_confirms": [{"token": token, "title": "Push"}]})
    service.decline(job, token)
    assert job.status == "queued" and job.meta["confirmations"] == []
    assert "declined" in service.next_query(job) and "Do not retry" in service.next_query(job)


def test_children_wait_and_resume_parent():
    parent = _job(role="planner")
    parent.status = "running"
    kid1 = service.new_job(parent.user_id, agent=_agent(), prompt="build", kind="delegate", parent=parent)
    kid2 = service.new_job(parent.user_id, agent=_agent("Reviewer"), prompt="review", role="reviewer", kind="delegate", parent=parent)
    service.apply_outcome(parent, "plan", {"text": "Delegated", "jobs_started": [{"id": str(kid1.id)}, str(kid2.id)]})
    assert parent.status == "waiting_children" and set(parent.meta["children"]) == {str(kid1.id), str(kid2.id)}
    kid1.status, kid1.result = "done", "PR https://github.com/a/b/pull/9"
    assert service.resume_parent(parent, [kid1, kid2]) == []  # kid2 still queued
    kid2.status, kid2.error = "failed", "no access"
    events = service.resume_parent(parent, [kid1, kid2])
    assert parent.status == "queued" and events
    q = service.next_query(parent)
    assert "pull/9" in q and "[failed]" in q and "no access" in q


def test_cancel_clears_pending():
    job = _job()
    job.meta["pending"] = [{"token": "t"}]
    assert service.cancel(job)[0].data["status"] == "cancelled"
    assert job.status == "cancelled" and job.meta["pending"] == []
    assert service.cancel(job) == []


def test_fail_marks_failed():
    job = _job()
    ev = service.fail(job, "boom")
    assert job.status == "failed" and job.error == "boom" and ev.kind == "error"


def test_job_query_adds_role_brief_on_first_run_only():
    from app.jobs.runner import job_query

    job = _job(role="reviewer", prompt="Review a/b#4")
    q = job_query(job)
    assert "reviewer" in q and "Never merge" in q and "Review a/b#4" in q
    job.meta["history"] = [{"role": "user", "content": q}]
    job.meta["next_query"] = "continue"
    assert job_query(job) == "continue"


class _FakeSession:
    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def add(self, obj):
        self.store.append(obj)

    async def get(self, _model, _id):
        return None

    async def commit(self):
        return None


async def test_job_progress_writes_events_and_plan():
    from app.jobs.runner import JobProgress

    store: list = []
    job = _job()
    rep = JobProgress(job, lambda: _FakeSession(store), agent_name="Coder")
    await rep.emit("Reading the repo", kind="tool", tool="sandbox_shell")
    await rep.emit("Reading the repo", kind="tool", tool="sandbox_shell")  # deduped
    await rep.emit("Plan: 1) fix 2) test", kind="tool", tool="write_todos")
    await rep.child("Reviewer").emit("Looking at the PR")
    await rep.record("test", "Tests passed", {"passed": True})
    assert [(e.kind, e.text) for e in store] == [
        ("step", "Reading the repo"),
        ("plan", "Plan: 1) fix 2) test"),
        ("step", "Reviewer: Looking at the PR"),
        ("test", "Tests passed"),
    ]
    assert all(e.root_id == job.id for e in store)


async def test_git_tools_record_test_and_pr_events(monkeypatch):
    from app.agent import git_tools

    recorded = []

    class Rec:
        async def record(self, kind, text, data):
            recorded.append((kind, text, data))

    ctx = SimpleNamespace(progress=Rec(), confirmations=[])

    async def fake_exec(_ctx, script, timeout=0, token=None):
        return {"exit_code": 0, "output": "5 passed"}

    async def fake_push(_ctx, args):
        return {"repo": "a/b", "branch": "agent/x", "pushed": True, "pr_url": "https://github.com/a/b/pull/2"}

    monkeypatch.setattr(git_tools, "sandbox_exec", fake_exec)
    monkeypatch.setattr(git_tools, "run_push", fake_push)
    res = await git_tools.run_tests(ctx, "a/b")
    assert res["passed"] is True
    await git_tools.run_confirmed(ctx, "push_branch", {"repo": "a/b", "branch": "agent/x"})
    kinds = [k for k, _, _ in recorded]
    assert kinds == ["test", "pr"]
    assert recorded[1][2]["url"].endswith("/pull/2")
    # plain chat (no job reporter) is a no-op
    await git_tools.run_tests(SimpleNamespace(progress=None), "a/b")


@pytest.mark.parametrize(
    "text,want",
    [
        ("Fix the login bug in specialshweta26/Checkme and open a PR", True),
        ("Add dark mode to the settings page in the repo", True),
        ("https://github.com/a/b add a health check endpoint", True),
        ("I'd most like a hand with coding and deployments.", False),
        ("What does the repo do?", False),
        ("Add a meeting with Raj tomorrow", False),
        ("thanks!", False),
    ],
)
def test_looks_like_coding_task(text, want):
    assert looks_like_coding_task(text) is want


async def test_start_coding_job_tool_only_in_chat_for_coding_agents():
    from app.agent.deep import RunContext, harness_tools

    user = SimpleNamespace(id=uuid.uuid4(), timezone="Asia/Kolkata")
    coder = SimpleNamespace(id=uuid.uuid4(), name="Coder", focus="coding", kind="custom")
    chief = SimpleNamespace(id=uuid.uuid4(), name="Chief", focus="", kind="default")

    def names(**kw):
        ctx = RunContext(db=kw.pop("db", object()), user=user, connectors={}, **kw)
        return {t.name for t in harness_tools(ctx)}

    assert "start_coding_job" in names(agent=coder)
    assert "start_coding_job" not in names(agent=coder, job=SimpleNamespace(id=uuid.uuid4()))
    assert "start_coding_job" not in names(agent=coder, db=None)
    assert "start_coding_job" not in names(agent=chief)


async def test_chat_route_starts_job_for_coding_request(monkeypatch):
    from app.chat import routes
    from app.jobs import worker

    added = []

    class DB:
        def add(self, obj):
            added.append(obj)

        async def commit(self):
            return None

    monkeypatch.setattr(worker, "wake", lambda: None)
    user = SimpleNamespace(id=uuid.uuid4())
    sess = SimpleNamespace(id=uuid.uuid4(), title="x", updated_at=None)
    coder = SimpleNamespace(id=uuid.uuid4(), name="Coder", focus="coding", color="#2f6bff", title="software engineer")
    chief = SimpleNamespace(id=uuid.uuid4(), name="Chief", focus="")
    text = "Fix the flaky login test in specialshweta26/Checkme"
    args = (DB(), user, sess)
    assert await routes._maybe_start_job(*args, chief, None, [], text, text, [], "r1") is None
    assert await routes._maybe_start_job(*args, coder, SimpleNamespace(id=1), [], text, text, [], "r1") is None
    assert await routes._maybe_start_job(*args, coder, None, [{"tool": "x"}], text, text, [], "r1") is None
    out = await routes._maybe_start_job(*args, coder, None, [], text, text, [{"role": "user", "content": "hi"}], "r1")
    assert out["route"] == "job" and out["job"]["status"] == "queued" and out["job"]["session_id"] == str(sess.id)
    job = next(o for o in added if type(o).__name__ == "AgentJob")
    assert job.role == "planner" and "Recent conversation" in job.prompt
    reply = [o for o in added if type(o).__name__ == "ChatMessage" and o.role == "assistant"][0]
    assert reply.meta["job"]["id"] == str(job.id)
