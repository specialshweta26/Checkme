"""Per-task skill state: choosing, checkpointing, resuming, isolation and handover between agents."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from types import SimpleNamespace

from app.skills import task_state as ts

USER = SimpleNamespace(id=uuid.uuid4())
SKILL = {"id": str(uuid.uuid4()), "name": "Rent a bike in Anjuna", "instruction": "1. Open sukuto.\n2. Pick dates.\n3. Compare prices.", "updated_at": "2026-10-28T17:38:53+00:00"}
OTHER = {"id": str(uuid.uuid4()), "name": "File expenses", "instruction": "Open the sheet and add rows.", "steps": [{}, {}]}


class Progress:
    def __init__(self):
        self.lines: list[tuple[str, str, dict]] = []

    async def emit(self, text, kind="step", tool=""):
        self.lines.append(("emit", text, {}))


class JobProgress(Progress):
    async def record(self, kind, text, data=None):
        self.lines.append((kind, text, data or {}))


def test_count_steps_and_labels():
    assert ts.count_steps(SKILL) == 3
    assert ts.count_steps(OTHER) == 2
    assert ts.step_label({"step_index": 1, "step_count": 3}) == "step 2/3"
    assert ts.step_label({"step_index": 3, "step_count": 3}) == "all 3 steps done"


async def test_job_state_is_checkpointed_and_resumes_after_restart(task_store):
    key = ts.task_key_for(job_id="j1")
    state, resumed = await ts.begin(USER, key, agent_id="a1", skill=SKILL, job_id=None)
    assert not resumed and state["skill_version"].startswith("2026-10-28") and state["step_count"] == 3
    await ts.checkpoint(state["id"], step_index=1, note="Opened sukuto", artifact={"label": "url", "value": "https://sukuto.com"}, by="tony")
    # A restart: nothing in memory, the job's next attempt routes nothing (or the same skill) and resumes.
    again, resumed = await ts.begin(USER, key, agent_id="a1", skill=None)
    assert resumed and again["id"] == state["id"] and again["step_index"] == 1
    assert again["notes"][0]["text"] == "Opened sukuto" and again["artifacts"][0]["value"] == "https://sukuto.com"
    block = ts.prompt_block(again, resumed=True)
    assert "Rent a bike in Anjuna" in block and "1 of 3 steps done" in block and "do not redo" in block and "skill_progress" in block
    # A job keeps its skill even when routing picks a different one on a later attempt.
    kept, resumed = await ts.begin(USER, key, skill=OTHER)
    assert resumed and kept["id"] == state["id"]
    # Step index never runs past the skill.
    assert (await ts.checkpoint(state["id"], step_index=9))["step_index"] == 3


async def test_concurrent_tasks_are_isolated(task_store):
    a, _ = await ts.begin(USER, ts.task_key_for(job_id="ja"), skill=SKILL)
    b, _ = await ts.begin(USER, ts.task_key_for(session_id="s1", agent_id="a1"), skill=SKILL)
    c, _ = await ts.begin(USER, ts.task_key_for(session_id="s1", agent_id="a2"), skill=OTHER)
    await ts.checkpoint(a["id"], step_index=2)
    assert len({a["id"], b["id"], c["id"]}) == 3
    assert (await task_store.get(b["id"]))["step_index"] == 0
    assert (await task_store.get(c["id"]))["skill_name"] == "File expenses"
    other_user, _ = await ts.begin(SimpleNamespace(id=uuid.uuid4()), ts.task_key_for(job_id="ja"), skill=None)
    assert other_user is None


async def test_chat_task_switches_resumes_and_ignores_meta_questions(task_store):
    key = ts.task_key_for(session_id="s1", agent_id="a1")
    first, _ = await ts.begin(USER, key, skill=SKILL)
    follow, resumed = await ts.begin(USER, key, skill=None)
    assert resumed and follow["id"] == first["id"]
    meta, resumed = await ts.begin(USER, key, skill=None, meta_question=True)
    assert meta is None and not resumed
    switched, resumed = await ts.begin(USER, key, skill=OTHER)
    assert not resumed and switched["skill_name"] == "File expenses"
    assert (await task_store.get(first["id"]))["status"] == "dropped"
    # Old chat state does not come back after the resume window.
    task_store.rows[switched["id"]]["updated_at"] -= timedelta(hours=ts.CHAT_RESUME_HOURS + 1)
    stale, _ = await ts.begin(USER, key, skill=None)
    assert stale is None


async def test_confirm_skills_uses_llm_with_json_and_falls_back():
    seen = {}

    async def llm(messages, response_format=None, max_tokens=0):
        seen["messages"] = messages
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"use": [SKILL["id"]]})))])

    picked = await ts.confirm_skills("rent me a scooter in anjuna", [OTHER, SKILL], llm=llm)
    assert [p["name"] for p in picked] == ["Rent a bike in Anjuna"] and picked[0]["match"]["confirmed"] is True
    assert "JSON" in seen["messages"][-1]["content"]

    async def none(messages, **_):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"use": []}'))])

    assert await ts.confirm_skills("what skills do you have", [SKILL], llm=none) == []

    async def broken(messages, **_):
        raise RuntimeError("down")

    kept = await ts.confirm_skills("rent a bike", [SKILL], llm=broken)
    assert kept[0]["id"] == SKILL["id"] and kept[0]["match"]["confirmed"] is None


async def test_handover_carries_state_and_result_returns(task_store):
    parent, _ = await ts.begin(USER, ts.task_key_for(session_id="s1", agent_id="tony"), agent_id="tony", skill=SKILL)
    await ts.checkpoint(parent["id"], step_index=1, note="Shortlisted 2 shops", artifact={"label": "shops", "value": "sukuto, rentmybike"})
    parent = await task_store.get(parent["id"])
    child = await ts.handover(parent, "riya", "peer:x:riya", by="tony", to_name="riya")
    assert child["parent_id"] == parent["id"] and child["step_index"] == 1 and child["skill_id"] == SKILL["id"]
    assert child["artifacts"][0]["label"] == "shops" and "Handed over by tony" in child["notes"][-1]["text"]
    assert (await task_store.get(parent["id"]))["status"] == "handed_off"
    # The receiver resumes from the handed state (its own key), even if its routing finds another skill.
    got, resumed = await ts.begin(USER, "peer:x:riya", skill=OTHER)
    assert resumed and got["id"] == child["id"]
    assert "handed this task to you" in ts.prompt_block(got)
    await ts.checkpoint(child["id"], step_index=2, artifact={"label": "quote", "value": "₹350/day"}, by="riya")
    await ts.finish(child["id"], "Sukuto has scooters at ₹350/day.", by="riya")
    back = await task_store.get(parent["id"])
    assert back["status"] == "active" and back["step_index"] == 2
    assert "Result from riya: Sukuto" in back["notes"][-1]["text"]
    assert any(a["label"] == "quote" for a in back["artifacts"])
    assert (await task_store.get(child["id"]))["status"] == "done"


async def test_finished_delegated_job_writes_back(task_store):
    parent, _ = await ts.begin(USER, ts.task_key_for(job_id="root"), skill=SKILL)
    child = await ts.handover(parent, "coder", ts.task_key_for(job_id="kid"), by="tony", to_name="coder", job_id="kid")
    job = SimpleNamespace(id="kid", user_id=USER.id, status="done", result="PR #40 opened", error="", meta={"agent_name": "coder"})
    await ts.finish_job(job)
    assert (await task_store.get(child["id"]))["status"] == "done"
    assert "PR #40 opened" in (await task_store.get(parent["id"]))["notes"][-1]["text"]
    failed = SimpleNamespace(id="nothing", user_id=USER.id, status="failed", result="", error="x", meta={})
    await ts.finish_job(failed)  # no state: no error


async def test_agent_helpers_narrate_and_hand_over(task_store):
    from app.agent import deep

    progress = Progress()
    ctx = deep.RunContext(db=object(), user=USER, connectors={}, agent=SimpleNamespace(id="tony", name="tony"), progress=progress)
    ctx.task_key = ts.task_key_for(session_id="s1", agent_id="tony")
    await deep._begin_task_state(ctx, SKILL)
    assert ctx.task_state["skill_name"] == SKILL["name"] and ctx.active_skills[0]["id"] == SKILL["id"]
    assert progress.lines[-1][1] == "Using skill: Rent a bike in Anjuna · step 1/3"
    handed = await deep._hand_over_state(ctx, "riya", "riya", "peer:k:riya")
    assert handed["parent_id"] == ctx.task_state["id"] and ctx.task_state["status"] == "handed_off"
    assert "Handing skill Rent a bike in Anjuna (step 1/3) to riya" in progress.lines[-1][1]
    await deep._take_back_state(ctx, handed["id"], "done: booked", "riya")
    assert ctx.task_state["status"] == "active" and "result back from riya" in progress.lines[-1][1]
    out = deep._task_state_out(ctx)
    assert out["skill_name"] == SKILL["name"] and "user_id" not in out

    jp = JobProgress()
    jctx = deep.RunContext(db=object(), user=USER, connectors={}, progress=jp, job=SimpleNamespace(id="j9"))
    jctx.task_key = ts.task_key_for(job_id="j9")
    await deep._begin_task_state(jctx, SKILL)
    kind, text, data = jp.lines[-1]
    assert kind == "status" and text.startswith("Using skill:") and data["skill_state"]["step_count"] == 3
    jctx2 = deep.RunContext(db=object(), user=USER, connectors={}, progress=jp, job=SimpleNamespace(id="j9"))
    jctx2.task_key = jctx.task_key
    await deep._begin_task_state(jctx2, None)
    assert jctx2.task_resumed and jp.lines[-1][1].startswith("Resuming skill:")


def test_task_states_route(task_store):
    import asyncio

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.deps import current_user
    from app.skills import routes

    asyncio.run(ts.begin(USER, ts.task_key_for(job_id="j1"), skill=SKILL))
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[current_user] = lambda: USER
    client = TestClient(app)
    data = client.get(f"/skills/tasks/states?job_id={uuid.uuid4()}").json()
    assert data["states"][0]["skill_name"] == SKILL["name"]
    assert client.get("/skills/tasks/states?session_id=nope").status_code == 400
