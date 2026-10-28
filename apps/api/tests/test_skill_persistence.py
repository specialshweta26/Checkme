"""Taught skills are saved while recording, survive restarts, and always end up readable."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from app.skills import narration, recorder, service

USER = SimpleNamespace(id="00000000-0000-0000-0000-0000000000c1")


@pytest.fixture
def quiet(monkeypatch):
    settings = SimpleNamespace(teach_max_seconds=600, openai_api_key="k", teach_audio_max_bytes=10_000)
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    monkeypatch.setattr(narration, "get_settings", lambda: settings)
    monkeypatch.setattr(recorder, "start", lambda *a: None)
    monkeypatch.setattr(recorder, "peek", lambda key: {"events": [{"t": 1.0, "kind": "click", "x": 5, "y": 6, "window": "Login - Example", "source": "screen"}], "frames": [{"t": 1.0, "jpeg": b"\xff\xd8jpg"}], "notes": []})

    async def stop(key):
        return {"events": [{"t": 1.0, "kind": "click", "x": 5, "y": 6, "window": "Login - Example", "source": "screen"}], "frames": [{"t": 1.0, "jpeg": b"\xff\xd8jpg"}], "notes": []}

    monkeypatch.setattr(recorder, "stop", stop)


async def test_steps_are_persisted_while_teaching(skill_store, quiet):
    started = await service.start_teaching(USER, None)
    sid = started["skill_id"]
    assert skill_store.rows[sid]["status"] == "recording"
    service.record_user_input(USER.id, "input", text="anjuna bike rental")
    await service._flush(str(USER.id), service._TEACH[str(USER.id)])
    row = skill_store.rows[sid]
    assert [s["kind"] for s in row["steps"]] == ["click", "input"] or {s["kind"] for s in row["steps"]} == {"click", "input"}
    assert row["frames"] and row["frames"][0]["jpeg"]  # base64 screenshot saved
    assert row["status"] == "recording"


async def test_stop_returns_immediately_and_falls_back_when_the_writer_fails(skill_store, quiet, monkeypatch):
    async def broken(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr(service, "_write_skill", broken)
    started = await service.start_teaching(USER, None)
    service.record_user_input(USER.id, "input", text="hello world")
    out = await service.stop_teaching(None, USER, "")
    assert out["status"] == "writing" and out["id"] == started["skill_id"]
    assert skill_store.rows[out["id"]]["status"] == "writing"  # visible right away, before the job runs
    await skill_store.settle()
    row = skill_store.rows[out["id"]]
    assert row["status"] == "ready"
    assert "model down" in row["error"]
    assert "Recorded steps:" in row["instruction"] and "hello world" in row["instruction"]
    assert row["name"] == "Workflow: Login"  # renamed from the default


async def test_empty_recording_is_not_kept(skill_store, quiet, monkeypatch):
    async def stop(key):
        return {"events": [], "frames": [], "notes": []}

    monkeypatch.setattr(recorder, "stop", stop)
    started = await service.start_teaching(USER, None)
    out = await service.stop_teaching(None, USER, "")
    await skill_store.settle()
    assert started["skill_id"] not in skill_store.rows and out["id"] == started["skill_id"]


async def test_stop_after_restart_recovers_the_saved_draft(skill_store, quiet, monkeypatch):
    async def writer(actions, frames, hint, transcript=None):
        return {"name": "Rent a bike", "instruction": "1. Open the site."}

    monkeypatch.setattr(service, "_write_skill", writer)
    sid = await skill_store.create(USER.id, None, "Taught workflow", True)
    await skill_store.update(sid, steps=[{"t": 1, "kind": "click", "x": 1, "y": 2}])
    service._TEACH.clear()  # the API restarted: in-memory session is gone
    out = await service.stop_teaching(None, USER, "")
    assert out["recovered"] is True and out["id"] == sid
    await skill_store.settle()
    row = skill_store.rows[sid]
    assert row["status"] == "ready" and row["name"] == "Rent a bike"
    assert any("server restarted" in n for n in row["notes"])


async def test_writer_puts_json_in_the_input(monkeypatch):
    seen = {}

    class Responses:
        async def create(self, **kw):
            seen.update(kw)
            return SimpleNamespace(output_text='{"name": "N", "instruction": "1. Do it."}')

    import app.llm as llm

    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(openai_api_key="k", openai_model="m"))
    monkeypatch.setattr(llm, "openai_client", lambda: SimpleNamespace(responses=Responses()))
    monkeypatch.setattr(llm, "reasoning_payload", lambda: None)
    out = await service._write_skill([{"t": 1, "kind": "click", "x": 1, "y": 1}], [{"t": 1, "jpeg": "AAAA"}], "", [])
    assert out == {"name": "N", "instruction": "1. Do it."}
    text = " ".join(part.get("text", "") for part in seen["input"][0]["content"])
    assert "json" in text.lower(), "the Responses API rejects json_object unless the input says JSON"


def test_secrets_are_masked():
    rows = service.mask_secrets(
        [
            {"t": 1, "kind": "window", "window": "Sign in - Bank"},
            {"t": 2, "kind": "type", "text": "anjuna scooter"},
            {"t": 3, "kind": "type", "text": "Hunter2!x"},
            {"t": 4, "kind": "input", "text": "482913"},
            {"t": 5, "kind": "input", "text": "4111 1111 1111 1111"},
            {"t": 6, "kind": "input", "text": "me@example.com"},
        ]
    )
    assert rows[1]["text"] == "anjuna scooter"
    for row in rows[2:5]:
        assert row["masked"] and "hidden" in row["text"]
    assert rows[5]["text"] == "me@example.com"


def test_kill_pattern_does_not_kill_its_own_shell():
    # `pkill -f /tmp/checkme_teach.py` matched the bash -c running it and killed it (exit 143).
    done = subprocess.run(["bash", "-c", recorder.KILL_CAPTURE], capture_output=True, timeout=10)
    assert done.returncode == 0


def test_step_list_and_auto_name():
    steps = [{"t": 2, "kind": "click", "x": 1, "y": 2, "window": "Sukuto - Bike rental", "said": "open rentals"}]
    text = service.step_list_instruction(steps, [], [])
    assert "1. [00:02]" in text and "open rentals" in text
    assert service.auto_name(steps, []) == "Workflow: Sukuto"
    assert service.auto_name(steps, [{"text": "Find a scooter in Anjuna"}]) == "Find a scooter in Anjuna"


def test_serialize_never_inlines_screenshots():
    row = SimpleNamespace(
        id="1", agent_id=None, name="x", kind="taught", status="ready", error="", steps=[], instruction="", duration_ms=0,
        created_at=None, updated_at=None, transcript=[{"t": 1, "text": "hi"}], frames=[{"t": 1, "jpeg": "AAAA"}], notes=[],
    )
    full = service.serialize_skill(row, full=True)
    assert full["frames"] == [{"index": 0, "t": 1}] and "AAAA" not in str(full)
    assert service.serialize_skill(row)["frame_count"] == 1


def test_skill_detail_frame_and_rewrite_routes(monkeypatch):
    import uuid

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.deps import current_user
    from app.skills import routes

    sid = uuid.uuid4()
    row = SimpleNamespace(
        id=sid, agent_id=None, name="Rent a bike", kind="taught", status="ready", error="", instruction="1. Go.",
        steps=[{"t": 1, "kind": "click", "x": 1, "y": 2}], duration_ms=5000, created_at=None, updated_at=None,
        transcript=[{"t": 1, "text": "open it"}], frames=[{"t": 1, "jpeg": "/9j/"}], notes=[],
    )

    async def fake_get(db, user, skill_id="", name="", agent_id=None):
        return row if str(skill_id) == str(sid) else None

    async def fake_rewrite(db, user, skill_id):
        return {**service.serialize_skill(row), "status": "writing"}

    monkeypatch.setattr(routes.service, "get_skill", fake_get)
    monkeypatch.setattr(routes.service, "rewrite_skill", fake_rewrite)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=uuid.uuid4())
    app.dependency_overrides[get_db] = lambda: None
    client = TestClient(app)
    got = client.get(f"/skills/{sid}").json()
    assert got["name"] == "Rent a bike" and got["transcript"][0]["text"] == "open it" and got["frames"] == [{"index": 0, "t": 1}]
    img = client.get(f"/skills/{sid}/frames/0")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg" and img.content.startswith(b"\xff\xd8")
    assert client.get(f"/skills/{sid}/frames/3").status_code == 404
    assert client.get(f"/skills/{uuid.uuid4()}").status_code == 404
    assert client.post(f"/skills/{sid}/rewrite").json()["status"] == "writing"
