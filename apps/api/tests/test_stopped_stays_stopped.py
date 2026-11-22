"""A stopped request stays stopped: "hy" after Stop is a greeting, not a request to pick the stopped task up again."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent import cancel
from app.agent.fast_path import is_small_talk
from app.chat import quick
from app.chat.routes import _after_stop_note


@pytest.mark.parametrize("text", ["hy", "Hyy", "hii", "heyy", "hlo", "helo", "hello!", "hiya", "hola", "hi there"])
def test_greetings_with_any_spelling_are_small_talk(text):
    assert is_small_talk(text) and quick.instant(text, uploads=False, confirms=False, group=False, resuming=False)


@pytest.mark.parametrize("text", ["hyderabad sales", "hi, how did one8 do last week?", "help me"])
def test_real_requests_are_not(text):
    assert not is_small_talk(text)


def _rows(stopped: bool):
    asked = SimpleNamespace(role="user", request_id="r1", content="In One8, how are Seam shoes doing?", meta={})
    reply = SimpleNamespace(role="assistant", request_id="r1", content="⏹ Stopped.", meta={"stopped": True} if stopped else {})
    return [reply, asked]  # newest first, as the chat route loads them


def test_a_new_message_after_stop_says_the_stopped_request_stays_stopped():
    out = _after_stop_note("what's on my calendar today?", _rows(stopped=True))
    assert out.startswith("what's on my calendar today?") and "stays stopped" in out
    assert "Seam" not in out  # the stopped request's text would steer routing back to it
    assert _after_stop_note("what's on my calendar today?", _rows(stopped=False)) == "what's on my calendar today?"


def test_an_interruption_answers_the_new_message():
    text = cancel.resume_prompt("In One8, how are Seam shoes doing?", "", ["Reading rows"], interrupted_by="hy")
    assert "a greeting or an unrelated question is not a request to continue" in text
