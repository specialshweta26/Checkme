"""The learning loop (docs/QUALITY.md "Lessons"): a thumbs-down becomes a lesson the user can accept, and accepted
lessons go into the agent's prompt without touching its own instructions."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.quality import lessons
from tests.test_quality import URL, db, needs_db  # noqa: F401 - the shared database fixture


def _llm(answer):
    calls = []

    async def llm(messages, **kw):
        calls.append(json.loads(messages[1]["content"].split("\n\n")[0]))
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"lesson": answer})))])

    llm.calls = calls
    return llm


def test_prompt_lists_the_newest_lessons_after_the_instructions():
    agent = SimpleNamespace(lessons=[{"id": str(i), "text": f"Rule {i}"} for i in range(15)])
    block = lessons.lessons_prompt(agent)
    assert block.startswith("Lessons from this user's feedback")
    assert "- Rule 14" in block and "- Rule 3" in block and "- Rule 2\n" not in block  # newest 12 only
    assert lessons.lessons_prompt(SimpleNamespace(lessons=[])) == ""


async def test_draft_uses_the_model_and_falls_back_to_the_note():
    llm = _llm("When asked for an inbox summary, list which emails need a reply, not just counts.")
    text = await lessons.draft("summarise my inbox", "12 new emails", "Incomplete", "say which need replies", [], llm=llm)
    assert text.startswith("When asked for an inbox summary") and llm.calls[0]["user_note"] == "say which need replies"
    # Already known: nothing new.
    assert await lessons.draft("x", "y", "", "", [text], llm=_llm(text)) == ""
    # Model down: the user's own note is the lesson; with no note there's nothing to learn.
    assert await lessons.draft("x", "y", "Wrong", "Always quote the source page", [], llm=_llm(RuntimeError("down"))) == "Always quote the source page"
    assert await lessons.draft("x", "y", "Too slow", "", [], llm=_llm(RuntimeError("down"))) == ""


@needs_db
async def test_propose_accept_edit_remove_and_add(db, monkeypatch):  # noqa: F811
    from app.models import User, WorkspaceAgent
    from app.quality import service

    monkeypatch.setattr(lessons, "propose_later", lambda feedback_id: None)
    factory, uid, other, agent_id, sid = db
    async with factory() as s:
        me, stranger = await s.get(User, uid), await s.get(User, other)
        await service.rate(s, me, session_id=sid, request_id="r1", rating=-1, reasons=["wrong"], note="Use the finance export, not memory")
        fb = (await service.list_feedback(s, me, rating=-1))[0]
        row = await lessons.propose(s, fb["id"], llm=_llm("Take revenue figures from the finance export, never from memory."))
        assert row is not None and row.status == "proposed" and "finance export" in row.text
        waiting = await lessons.list_lessons(s, me, status="proposed")
        assert [w["agent_name"] for w in waiting] == ["Marki"] and "Wrong or made up" in waiting[0]["why"]
        assert await lessons.list_lessons(s, stranger) == []
        with pytest.raises(lessons.LessonError):
            await lessons.accept(s, stranger, row.id)
        # Accept with an edit: the agent's lessons change, its own instructions don't.
        await lessons.accept(s, me, row.id, "Take revenue figures from the finance export.")
        agent = await s.get(WorkspaceAgent, agent_id)
        await s.refresh(agent)
        assert [x["text"] for x in agent.lessons] == ["Take revenue figures from the finance export."]
        assert (agent.instructions or "") == ""
        assert "- Take revenue figures from the finance export." in lessons.lessons_prompt(agent)
        manual = await lessons.add(s, me, agent_id, "Answer in INR.")
        await s.refresh(agent)
        assert len(agent.lessons) == 2 and manual["status"] == "accepted"
        await lessons.remove(s, me, row.id)
        await s.refresh(agent)
        assert [x["text"] for x in agent.lessons] == ["Answer in INR."]
        # A new thumbs-down proposal is withdrawn when the rating is cleared.
        await service.rate(s, me, session_id=sid, request_id="r0", rating=-1, note="Greet me by name")
        fb0 = next(f for f in await service.list_feedback(s, me, rating=-1) if f["request_id"] == "r0")
        await lessons.propose(s, fb0["id"], llm=_llm("Greet the user by name."))
        assert len(await lessons.list_lessons(s, me, status="proposed")) == 1
        await service.rate(s, me, session_id=sid, request_id="r0", rating=0)
        assert await lessons.list_lessons(s, me, status="proposed") == []
