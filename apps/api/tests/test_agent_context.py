"""Requests stay in their own context: skills don't hijack them, and attached files are worked on directly."""

from __future__ import annotations

import base64
import io
import json
import uuid
import zipfile
from types import SimpleNamespace

import pytest
from PIL import Image

from app.agent import attachments, finish_check
from app.skills import task_state as ts
from app.storage import extract_text

STATE = {"id": "s1", "skill_id": "k1", "skill_name": "Generate OMS Order Status Report", "status": "active"}


def _llm(*answers):
    calls = []

    async def llm(messages, **kw):
        calls.append(messages)
        answer = answers[len(calls) - 1]
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=answer))])

    llm.calls = calls
    return llm


@pytest.mark.parametrize("text", ["continue", "Go on please", "next step", "resume"])
async def test_a_clear_carry_on_needs_no_model_call(text):
    assert await ts.continues_task(text, STATE, llm=_llm())


async def test_the_model_decides_other_messages():
    assert not await ts.continues_task("list folder in my desktop", STATE, "Which date?", _llm('{"continues": false}'))
    assert await ts.continues_task("5 November", STATE, "Which date?", _llm('{"continues": true}'))


@pytest.mark.parametrize("answer", ["not json", '{"continues": "yes"}', "{}", RuntimeError("down")])
async def test_if_unsure_it_is_a_new_request(answer):
    assert not await ts.continues_task("hello", STATE, llm=_llm(answer))


class _Store:
    def __init__(self, current):
        self.current, self.updates = current, []

    async def find_open(self, user_id, key):
        return self.current

    async def update(self, state_id, **fields):
        self.updates.append(fields)


async def test_an_unrelated_message_does_not_resume_the_skill_or_close_it(monkeypatch):
    from datetime import datetime, timezone

    store = _Store({**STATE, "updated_at": datetime.now(timezone.utc)})
    monkeypatch.setattr(ts, "STORE", store)

    async def no(*a, **k):
        return False

    monkeypatch.setattr(ts, "continues_task", no)
    assert await ts.begin(SimpleNamespace(id=uuid.uuid4()), "chat:1", query="hello") == (None, False)
    assert store.updates == []


async def test_a_message_that_carries_on_resumes_it(monkeypatch):
    from datetime import datetime, timezone

    store = _Store({**STATE, "updated_at": datetime.now(timezone.utc)})
    monkeypatch.setattr(ts, "STORE", store)

    async def yes(*a, **k):
        return True

    monkeypatch.setattr(ts, "continues_task", yes)
    state, resumed = await ts.begin(SimpleNamespace(id=uuid.uuid4()), "chat:1", query="use yesterday")
    assert state["id"] == "s1" and resumed is True


def test_only_this_users_images_are_used():
    me, other = uuid.uuid4(), uuid.uuid4()
    uploads = [
        {"key": f"{me}/a/shot.png", "filename": "shot.png"},
        {"key": f"{me}/b/photo", "filename": "photo", "content_type": "image/jpeg"},
        {"key": f"{other}/c/secret.png", "filename": "secret.png"},
        {"key": f"{me}/d/report.pdf", "filename": "report.pdf"},
    ]
    assert attachments.uploaded_images(me, uploads) == [f"{me}/a/shot.png", f"{me}/b/photo"]
    many = [{"key": f"{me}/{i}.png", "filename": f"{i}.png"} for i in range(9)]
    assert len(attachments.uploaded_images(me, many)) == attachments.MAX_IMAGES


def _png(size=(3000, 1000)) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", size, (255, 0, 0, 255)).save(out, "PNG")
    return out.getvalue()


async def test_the_model_is_shown_the_image_scaled_down(monkeypatch):
    monkeypatch.setattr(attachments, "get_bytes", lambda key: (_png(), "image/png"))
    message = await attachments.image_message(["u/a.png"])
    url = message.content[1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    with Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as img:
        assert max(img.size) == 1600


async def test_an_unreadable_image_does_not_stop_the_turn(monkeypatch):
    monkeypatch.setattr(attachments, "get_bytes", lambda key: (b"not an image", "image/png"))
    assert await attachments.image_message(["u/a.png"]) is None


def test_pptx_text_comes_back_in_slide_order():
    a = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for n, words in ((10, "Tenth"), (2, "Second"), (1, "First title")):
            z.writestr(f"ppt/slides/slide{n}.xml", f"<p:sld xmlns:p='x' {a}><a:t>{words}</a:t><a:t>body</a:t></p:sld>")
        z.writestr("ppt/slides/_rels/slide1.xml.rels", "<x/>")
    text = extract_text("Farming.PPTX", "", out.getvalue())
    assert text.splitlines() == ["Slide 1: First title body", "Slide 2: Second body", "Slide 10: Tenth body"]


def test_finish_check_prompt_trusts_links_and_stated_limits():
    assert "link" in finish_check._SYSTEM and "If in doubt" in finish_check._SYSTEM
    assert json.loads('{"complete": true}') and "Answer in JSON" in finish_check._SYSTEM
