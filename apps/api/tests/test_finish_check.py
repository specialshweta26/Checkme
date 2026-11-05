"""A finished turn is compared with the request; what's left is continued once, then reported."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.agent import deep, finish_check


def _ctx(**kw):
    base = {"teammate_depth": 0, "pending": [], "question": None, "used_tools": ["a", "b"]}
    return SimpleNamespace(**{**base, **kw})


def test_should_check_only_finished_top_level_work():
    assert finish_check.should_check(_ctx(), "done")
    assert not finish_check.should_check(_ctx(), "")
    assert not finish_check.should_check(_ctx(used_tools=["a"]), "done")
    assert not finish_check.should_check(_ctx(teammate_depth=1), "done")
    assert not finish_check.should_check(_ctx(pending=[{"token": "t"}]), "done")
    assert not finish_check.should_check(_ctx(question={"question": "which?"}), "done")


@pytest.mark.parametrize("raw", ["not json", "[]", '{"complete": true, "unfinished": ["x"]}', '{"unfinished": ["x"]}', '{"complete": false}', '{"complete": false, "unfinished": "x"}', '{"complete": false, "unfinished": ["  "]}'])
def test_unreadable_or_complete_verdicts_mean_nothing_left(raw):
    assert finish_check.parse_verdict(raw) == []


def test_verdict_lists_items_with_a_cap():
    raw = json.dumps({"complete": False, "unfinished": [f"item {i}" for i in range(9)]})
    assert finish_check.parse_verdict(raw) == [f"item {i}" for i in range(finish_check.MAX_ITEMS)]


async def test_a_failed_check_changes_nothing(monkeypatch):
    async def broken(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr("app.llm.chat_complete", broken)
    assert await finish_check.unfinished_items("goal", "reply", []) == []


async def test_the_request_meets_the_json_mode_rule(monkeypatch):
    seen = {}

    async def fake(messages, **kw):
        seen.update(messages=messages, **kw)
        content = '{"complete": false, "unfinished": ["email Ben"]}'
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    monkeypatch.setattr("app.llm.chat_complete", fake)
    assert await finish_check.unfinished_items("email both", "emailed Asha", ["send_email"]) == ["email Ben"]
    assert seen["response_format"] == {"type": "json_object"}
    assert any("json" in m["content"].lower() for m in seen["messages"] if m["role"] != "system")


class _Graph:
    def __init__(self, replies):
        self.replies = list(replies)
        self.inputs = []

    async def ainvoke(self, payload, config=None, **kw):
        self.inputs.append(payload)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {"messages": [SimpleNamespace(type="ai", content=reply, tool_calls=[])]}


async def _run(monkeypatch, verdicts, graph, ctx=None):
    calls = list(verdicts)

    async def fake_items(*a, **k):
        return calls.pop(0)

    monkeypatch.setattr(finish_check, "unfinished_items", fake_items)
    return await deep._finish_what_is_left(ctx or _ctx(), graph, "send both", "sent one", {"messages": []}, None, None, 80, {"configurable": {"thread_id": "t"}})


async def test_complete_turn_keeps_its_reply(monkeypatch):
    graph = _Graph([])
    assert await _run(monkeypatch, [[]], graph) == "sent one"
    assert graph.inputs == []


async def test_unfinished_work_continues_the_same_run_once(monkeypatch):
    graph = _Graph(["sent both"])
    assert await _run(monkeypatch, [["send the second"], []], graph) == "sent both"
    assert len(graph.inputs) == 1 and "send the second" in graph.inputs[0]["messages"][0]["content"]


async def test_the_run_continues_only_once_and_the_verdict_is_never_shown(monkeypatch):
    graph = _Graph(["tried"])
    out = await _run(monkeypatch, [["send the second"], ["send the second"]], graph)
    assert out == "tried" and len(graph.inputs) == 1


async def test_a_failed_continuation_keeps_the_reply_as_it_was(monkeypatch):
    out = await _run(monkeypatch, [["send the second"]], _Graph([RuntimeError("model down")]))
    assert out == "sent one"


async def test_stopping_to_ask_the_user_ends_the_check(monkeypatch):
    ctx = _ctx()

    class AskingGraph(_Graph):
        async def ainvoke(self, payload, config=None, **kw):
            ctx.question = {"question": "which one?"}
            return await super().ainvoke(payload, config, **kw)

    out = await _run(monkeypatch, [["send the second"]], AskingGraph(["Which one?"]), ctx)
    assert out == "Which one?"
