"""Stop / Continue / interrupt-and-redirect for chat turns and background jobs, and WhatsApp "/stop"."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.agent import cancel as run_cancel
from app.chat import routes as chat_routes


class _Result:
    def __init__(self, rows=None):
        self._rows = rows or []

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class FakeDB:
    def __init__(self):
        self.rows: list = []
        self.objects: dict = {}
        self.commits = 0
        self.rollbacks = 0

    def add(self, row):
        if getattr(row, "id", None) is None and hasattr(row, "__table__"):
            try:
                row.id = uuid.uuid4() if row.__table__.c.id.type.__class__.__name__ == "UUID" else len(self.rows) + 1
            except Exception:  # noqa: BLE001
                pass
        self.rows.append(row)
        if getattr(row, "id", None) is not None:
            self.objects[row.id] = row

    async def flush(self):
        for row in self.rows:
            if getattr(row, "id", None) is not None:
                self.objects[row.id] = row

    async def get(self, model, key):
        return self.objects.get(key)

    async def execute(self, *_a, **_k):
        return _Result()

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def refresh(self, _row):
        return None

    async def delete(self, row):
        pass


class FakeRedis:
    def __init__(self):
        self.kv: dict = {}
        self.lists: dict = {}

    async def rpush(self, k, v):
        self.lists.setdefault(k, []).append(v)

    async def expire(self, *_a):
        return True

    async def set(self, k, v, ex=None):
        self.kv[k] = v if isinstance(v, bytes) else str(v).encode()

    async def get(self, k):
        return self.kv.get(k)

    async def delete(self, *ks):
        for k in ks:
            self.kv.pop(k, None)

    async def lrange(self, k, *_a):
        return self.lists.get(k, [])


@pytest.fixture
def env(monkeypatch):
    redis = FakeRedis()
    import app.cache

    monkeypatch.setattr(app.cache, "get_redis", lambda: redis)

    async def not_limited(*_a, **_k):
        return False

    agent = SimpleNamespace(id=uuid.uuid4(), name="tony", avatar="", color="", title="web Researcher", focus="web Researcher")

    async def roster(db, user, agent_id, group_id):
        return agent, None, []

    async def no_job(*_a, **_k):
        return None

    async def no_jobs(*_a, **_k):
        return []

    import app.jobs.service as jobs

    monkeypatch.setattr(chat_routes, "rate_limited", not_limited)
    monkeypatch.setattr(chat_routes, "_resolve_roster", roster)
    monkeypatch.setattr(chat_routes, "_maybe_start_job", no_job)
    monkeypatch.setattr(chat_routes.agents_service, "session_belongs", lambda *a, **k: True)
    monkeypatch.setattr(jobs, "stop_chat_jobs", no_jobs)
    monkeypatch.setattr(run_cancel, "WATCH_EVERY_S", 0.05)
    calls: list = []

    async def slow_agent(db, user, session_id, request_id, history, query, confirms, **kw):
        calls.append(query)
        progress = kw.get("progress")
        if "quick" in query:
            return {"text": "Quick answer.", "used_tools": []}
        if progress is not None:
            await progress.emit("Searching the web for Vercel CLI docs", kind="tool")
            await progress.draft("Partial answer so far")
        for _ in range(400):
            await asyncio.sleep(0.01)
            run_cancel.check()  # what call_user_tool does between tool calls
        return {"text": "should not finish", "used_tools": []}

    monkeypatch.setattr(chat_routes, "run_deep_agent", slow_agent)
    user = SimpleNamespace(id=uuid.uuid4(), letta_agent_id="x", timezone="Asia/Kolkata")
    yield SimpleNamespace(db=FakeDB(), user=user, agent=agent, calls=calls, redis=redis)
    run_cancel._RUNS.clear()


async def _start(env, message, **kw):
    body = chat_routes.ChatIn(message=message, progress_id=kw.pop("progress_id", "turn_abcdef123456"), **kw)
    return asyncio.create_task(chat_routes.chat(body, SimpleNamespace(add_task=lambda *a, **k: None), env.user, env.db))


async def _wait_run(env, n=1):
    for _ in range(200):
        runs = run_cancel.find(env.user.id, kind="turn")
        if len(runs) >= n and all(r.task is not None for r in runs):
            return runs
        await asyncio.sleep(0.01)
    raise AssertionError("turn never registered")


def _assistant(env):
    return [r for r in env.db.rows if getattr(r, "role", None) == "assistant"]


@pytest.mark.asyncio
async def test_cancel_endpoint_stops_turn_and_keeps_partial_output(env):
    task = await _start(env, "Install vercel cli")
    (run,) = await _wait_run(env)
    await asyncio.sleep(0.05)
    out = await chat_routes.cancel_turn(chat_routes.CancelIn(progress_id="turn_abcdef123456"), env.user, env.db)
    assert out["ok"] and out["stopped"] == 1
    result = await asyncio.wait_for(task, 5)
    assert result["stopped"] is True and result["can_continue"] is True
    msg = _assistant(env)[-1]
    assert msg.meta["in_progress"] is False and msg.meta["stopped"] is True and msg.meta["can_continue"] is True
    assert msg.meta["pending_confirms"] == []
    assert "Partial answer so far" in msg.content and "Stopped" in msg.content
    assert any("Vercel CLI" in s for s in msg.meta["steps"])
    assert run_cancel.find(env.user.id) == []  # unregistered


@pytest.mark.asyncio
async def test_cancel_by_request_id_alias(env):
    task = await _start(env, "Install all requirements")
    (run,) = await _wait_run(env)
    out = await chat_routes.cancel_turn_by_id(run.request_id, env.user, env.db)
    assert out["stopped"] == 1
    assert (await asyncio.wait_for(task, 5))["stopped"] is True


@pytest.mark.asyncio
async def test_cancel_requires_a_target(env):
    with pytest.raises(chat_routes.HTTPException):
        await chat_routes.cancel_turn(chat_routes.CancelIn(), env.user, env.db)


@pytest.mark.asyncio
async def test_new_message_interrupts_and_redirects_with_context(env):
    first = await _start(env, "Research the Render CLI install steps")
    (run,) = await _wait_run(env)
    await asyncio.sleep(0.05)
    sess_id = uuid.UUID(run.session_id)
    second = await _start(env, "quick: use the npm route instead", session_id=sess_id, progress_id="turn_zzzzzz123456")
    assert (await asyncio.wait_for(first, 5))["stopped"] is True
    reply = await asyncio.wait_for(second, 5)
    assert reply["text"] == "Quick answer."
    redirected = env.calls[-1]
    assert "interrupted your previous turn" in redirected
    assert "Research the Render CLI install steps" in redirected
    assert "Searching the web for Vercel CLI docs" in redirected
    assert redirected.rstrip().endswith("quick: use the npm route instead")


@pytest.mark.asyncio
async def test_queue_mode_waits_instead_of_interrupting(env, monkeypatch):
    monkeypatch.setattr(chat_routes, "QUEUE_WAIT_S", 0.1)
    first = await _start(env, "Research something long")
    (run,) = await _wait_run(env)
    second = await _start(env, "quick follow-up", session_id=uuid.UUID(run.session_id), mode="queue", progress_id="turn_qqqqqq123456")
    reply = await asyncio.wait_for(second, 5)
    assert reply["text"] == "Quick answer."
    assert not run.cancelled and not first.done()
    run.cancel()
    await asyncio.wait_for(first, 5)


@pytest.mark.asyncio
async def test_interrupt_flag_off_queues(env, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_interrupt_on_new_message", False)
    monkeypatch.setattr(chat_routes, "QUEUE_WAIT_S", 0.1)
    first = await _start(env, "Research something long")
    (run,) = await _wait_run(env)
    await asyncio.wait_for(await _start(env, "quick", session_id=uuid.UUID(run.session_id), progress_id="turn_ffffff123456"), 5)
    assert not run.cancelled
    run.cancel()
    await asyncio.wait_for(first, 5)


@pytest.mark.asyncio
async def test_continue_resumes_from_the_stopped_turn(env, monkeypatch):
    stopped_msg = SimpleNamespace(role="assistant", content="Partial answer\n\n⏹ Stopped. Send a new message, or tap Continue to pick up where I left off.", meta={"stopped": True, "steps": ["Installed node 22"]})
    asked = SimpleNamespace(role="user", content="Install all requirements", meta={})

    async def execute(*_a, **_k):
        return _Result([asked, stopped_msg])

    sess = SimpleNamespace(id=uuid.uuid4())
    env.db.execute = execute
    ctx = await chat_routes._resume_context(env.db, sess, "req1")
    assert ctx == {"text": "Install all requirements", "partial": "Partial answer", "steps": ["Installed node 22"]}
    prompt = run_cancel.resume_prompt(ctx["text"], ctx["partial"], ctx["steps"])
    assert "continue it from where it stopped" in prompt and "Installed node 22" in prompt and "do not redo" in prompt


@pytest.mark.asyncio
async def test_cancel_with_nothing_running_unsticks_a_dead_turn(env):
    sess = SimpleNamespace(id=uuid.uuid4(), user_id=env.user.id)
    env.db.objects[sess.id] = sess
    stuck = SimpleNamespace(role="assistant", content="tony is working on this…", request_id="r1", meta={"in_progress": True, "progress_id": "turn_dead00000000"})

    async def execute(*_a, **_k):
        return _Result([stuck])

    env.db.execute = execute
    out = await chat_routes.cancel_turn(chat_routes.CancelIn(session_id=sess.id), env.user, env.db)
    assert out["stopped"] == 1
    assert stuck.meta["in_progress"] is False and stuck.meta["stopped"] is True
    assert "Stopped" in stuck.content
    assert any(k.endswith(str(sess.id)) for k in env.redis.kv)  # other API processes see the stop flag


# ---- cooperative cancellation primitives ----


@pytest.mark.asyncio
async def test_check_raises_only_for_the_stopped_run():
    run = run_cancel.register("k1", user_id="u")
    token = run_cancel.current.set(run)
    try:
        run_cancel.check()
        run.cancel()
        with pytest.raises(run_cancel.TurnCancelled):
            run_cancel.check()
    finally:
        run_cancel.current.reset(token)
        run_cancel.unregister("k1")
    run_cancel.check()  # no run in context: no-op


@pytest.mark.asyncio
async def test_call_user_tool_refuses_after_stop():
    from app.agent.deep import RunContext, call_user_tool

    run = run_cancel.register("k2", user_id="u")
    run.cancel()
    token = run_cancel.current.set(run)
    try:
        ctx = RunContext(db=None, user=SimpleNamespace(id="u"), connectors={})
        with pytest.raises(run_cancel.TurnCancelled):
            await call_user_tool(ctx, "computer", "exec", {"command": "ls"})
    finally:
        run_cancel.current.reset(token)
        run_cancel.unregister("k2")


@pytest.mark.asyncio
async def test_callback_raises_at_next_tool_or_model_step():
    run = run_cancel.register("k3", user_id="u")
    token = run_cancel.current.set(run)
    try:
        cb = run_cancel.callback()
        await cb.on_tool_start({}, "")
        run.cancel()
        with pytest.raises(run_cancel.TurnCancelled):
            await cb.on_chat_model_start({}, [])
    finally:
        run_cancel.current.reset(token)
        run_cancel.unregister("k3")


@pytest.mark.asyncio
async def test_stopped_cloud_command_is_killed_on_the_computer_not_the_sandbox(monkeypatch):
    from app.agent import computer_harness

    calls: list = []

    async def call_impl(slug, tool, args):
        calls.append(args["command"])
        if "pgrep" in args["command"]:
            return {"exit_code": 0, "stdout": ""}
        await asyncio.sleep(30)

    ctx = SimpleNamespace(
        connectors={"computer": SimpleNamespace(slug="computer", provider="daytona")}, call_impl=call_impl,
        user=SimpleNamespace(id="u"), pending=[], agent=None,
    )
    monkeypatch.setattr(computer_harness, "computer_slug", lambda c: "computer")

    async def run_computer(c, tool, args):
        return await call_impl("computer", tool, args)

    monkeypatch.setattr(computer_harness, "run_computer", run_computer)
    task = asyncio.create_task(computer_harness.cloud_exec(ctx, "npm install -g vercel", timeout=600))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) == 2
    tag = calls[0].rsplit(" ", 1)[-1]
    assert tag.startswith("checkme-run-")
    assert "pgrep -f '[c]heckme-run-" in calls[1] and tag[1:] in calls[1]
    assert "kill" in calls[1] and "e2b" not in calls[1].lower() and "shutdown" not in calls[1]


def test_kill_script_is_valid_bash():
    import subprocess

    from app.agent import toolchain

    script = toolchain.kill_script("abc123")
    assert subprocess.run(["bash", "-n", "-c", script]).returncode == 0
    assert subprocess.run(["bash", "-c", script], timeout=10).returncode == 0  # nothing to kill: still succeeds


def test_kill_script_kills_only_the_tagged_tree():
    import subprocess
    import time

    from app.agent import toolchain

    tagged = subprocess.Popen(["bash", "-c", toolchain.wrap("sleep 30; echo late", "t1")])
    other = subprocess.Popen(["bash", "-c", "sleep 30", "other-run"])
    try:
        time.sleep(0.3)
        subprocess.run(["bash", "-c", toolchain.kill_script("t1")], timeout=10)
        tagged.wait(timeout=5)
        assert tagged.returncode != 0
        assert other.poll() is None
    finally:
        for p in (tagged, other):
            if p.poll() is None:
                p.kill()


# ---- background jobs: cooperative stop in the job loop ----


@pytest.mark.asyncio
async def test_job_loop_stops_when_status_becomes_cancelled(monkeypatch):
    from app.jobs import worker

    monkeypatch.setattr(worker, "CANCEL_WATCH_S", 0.02)
    job = SimpleNamespace(status="running")

    class S:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, model, key):
            return job

    started = asyncio.Event()

    async def fake_run_job(job_id, session_factory):
        started.set()
        try:
            for _ in range(500):
                await asyncio.sleep(0.01)
                run_cancel.check()
        except (asyncio.CancelledError, run_cancel.TurnCancelled):
            assert run_cancel.is_cancelled()  # the job's own run is in context
            return "cancelled"
        return "done"

    async def no_beat(*a):
        await asyncio.sleep(10)

    monkeypatch.setattr(worker, "run_job", fake_run_job)
    monkeypatch.setattr(worker, "_heartbeat", no_beat)
    t = asyncio.create_task(worker._run_one(lambda: S(), "j1", "u1"))
    await started.wait()
    assert run_cancel.get(run_cancel.job_key("j1")) is not None
    job.status = "cancelled"  # e.g. POST /api/jobs/{id}/cancel from another process
    await asyncio.wait_for(t, 3)
    assert run_cancel.get(run_cancel.job_key("j1")) is None


@pytest.mark.asyncio
async def test_cancel_tree_stops_live_runs_and_clears_approvals(monkeypatch):
    from app.jobs import service

    root = SimpleNamespace(id="r", root_id="r", parent_id=None, user_id="u", status="waiting_approval", meta={"pending": [{"token": "t"}]}, finished_at=None, session_id=None)
    kid = SimpleNamespace(id="c", root_id="r", parent_id="r", user_id="u", status="running", meta={}, finished_at=None, session_id=None)

    class DB:
        added: list = []

        async def execute(self, *_a):
            return _Result([root, kid])

        def add(self, x):
            DB.added.append(x)

    monkeypatch.setattr(service, "event", lambda job, kind, text, data=None: (job.id, kind, text))
    run = run_cancel.register(run_cancel.job_key("c"), user_id="u", kind="job")
    changed = await service.cancel_tree(DB(), root, note=None)
    assert {j.id for j in changed} == {"r", "c"}
    assert root.status == kid.status == "cancelled" and root.meta["pending"] == []
    assert run.cancelled
    run_cancel.unregister(run.key)


# ---- WhatsApp ----


@pytest.mark.parametrize("text", ["stop", "/stop", "Stop!", "/cancel", "stop it"])
def test_whatsapp_stop_words_parse(text):
    from app.channels import service

    assert service.parse(text).kind == "stop"


def test_whatsapp_stop_is_not_a_normal_sentence():
    from app.channels import service

    assert service.parse("stop the deploy after tests pass").kind == "message"


@pytest.mark.asyncio
async def test_whatsapp_stop_cancels_routed_agents_job(monkeypatch):
    from app.channels import service
    import app.jobs.service as jobs

    agent = SimpleNamespace(id=uuid.uuid4(), name="tony")
    sess = SimpleNamespace(id=uuid.uuid4())
    user = SimpleNamespace(id=uuid.uuid4())
    sent: list = []
    stopped_for: list = []

    async def routed(db, u, link):
        return agent, None

    async def thread(db, u, a, g, fresh):
        return sess

    async def stop_chat_jobs(db, user_id, session_id, agent_id=None, since=None):
        stopped_for.append((user_id, session_id, agent_id))
        return [SimpleNamespace(id="j")]

    async def reply(link, text):
        sent.append(text)

    monkeypatch.setattr(service, "_routed_agent", routed)
    monkeypatch.setattr(service, "_thread", thread)
    monkeypatch.setattr(jobs, "stop_chat_jobs", stop_chat_jobs)
    monkeypatch.setattr(service, "reply", reply)
    db = FakeDB()
    assert await service.stop_running(db, user, SimpleNamespace()) == "stopped"
    assert stopped_for == [(user.id, sess.id, agent.id)]
    assert "Stopped" in sent[-1] and "tony" in sent[-1] and "continue" in sent[-1]
    assert db.rows and db.rows[-1].meta["stopped"] is True

    async def none(*a, **k):
        return []

    monkeypatch.setattr(jobs, "stop_chat_jobs", none)
    assert await service.stop_running(db, user, SimpleNamespace()) == "stop_idle"
    assert "Nothing is running" in sent[-1]
