"""Artifacts page (docs/DESIGN.md "Profile"): what tasks produced, newest first, one entry per item, only the
signed-in user's."""

from __future__ import annotations

import pytest

from app.skills import task_state


def _state(user, skill, artifacts, **extra):
    return {"user_id": user, "skill_name": skill, "status": "done", "artifacts": artifacts, "session_id": None, "job_id": None, "agent_id": None, **extra}


def test_feed_is_newest_first_and_drops_handover_copies():
    report = {"at": "2026-11-10T09:00:00+00:00", "label": "Report", "value": "https://example.com/r.pdf", "by": "Chief"}
    ticket = {"at": "2026-11-12T09:00:00+00:00", "label": "Ticket", "value": "OPS-12", "by": "Coder"}
    parent = _state("u", "Weekly digest", [report, ticket], session_id="s1")
    child = _state("u", "Weekly digest", [ticket])
    empty = _state("u", "Nothing", [{"at": "2026-11-13T00:00:00+00:00", "label": "", "value": ""}])

    feed = task_state.artifact_feed([parent, child, empty])

    assert [a["label"] for a in feed] == ["Ticket", "Report"]
    assert feed[1]["skill_name"] == "Weekly digest" and feed[1]["session_id"] == "s1"


@pytest.mark.asyncio
async def test_endpoint_lists_only_my_artifacts(task_store, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.deps import current_user
    from app.skills.routes import router

    class Me:
        id = "me"

    mine = {"at": "2026-11-12T09:00:00+00:00", "label": "Mine", "value": "1", "by": ""}
    theirs = {"at": "2026-11-12T09:00:00+00:00", "label": "Theirs", "value": "2", "by": ""}
    await task_store.create(_state("me", "A", [mine]))
    await task_store.create(_state("someone-else", "B", [theirs]))

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[current_user] = lambda: Me()
    body = TestClient(app).get("/api/skills/artifacts").json()

    assert [a["label"] for a in body["artifacts"]] == ["Mine"]
