"""Long browser runs keep the model's context small (docs/LLD.md "Context trimming")."""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent import context_trim as ct


def _page(n: int) -> str:
    return json.dumps({"ok": True, "url": f"https://travel.test/step{n}", "title": f"Step {n}", "text": "x" * 20000,
                       "tree": "\n".join(f"- e{i} button Item {i}" for i in range(900)), "warnings": ["w"] if n == 2 else []})  # fmt: skip


def _run(steps: int) -> list:
    msgs = [HumanMessage(content="flights from delhi to pune on 18 jan 2027")]
    for n in range(steps):
        msgs.append(AIMessage(content="", tool_calls=[{"id": f"c{n}", "name": "browser_act", "args": {}}]))
        msgs.append(ToolMessage(content=_page(n), tool_call_id=f"c{n}", name="browser_act"))
    return msgs


def test_only_the_latest_page_is_sent_in_full():
    msgs = _run(12)
    before = sum(len(m.content) for m in msgs if isinstance(m.content, str))
    out, saved = ct.trim_messages(msgs)
    after = sum(len(m.content) for m in out if isinstance(m.content, str))
    assert out[-1].content == msgs[-1].content  # the current page is untouched
    old = out[2].content
    assert old.startswith("[older page result, trimmed") and '"url": "https://travel.test/step0"' in old and len(old) < 1000
    assert '"warnings": ["w"]' in out[6].content  # warnings survive trimming
    assert after < before / 8 and saved == before - after
    assert msgs[2].content == _page(0)  # the stored conversation is not changed
    assert [type(m) for m in out] == [type(m) for m in msgs] and out[2].tool_call_id == "c0"


def test_other_big_results_are_cut_only_when_old():
    msgs = [HumanMessage(content="q"),
            AIMessage(content="", tool_calls=[{"id": "a", "name": "web_fetch", "args": {}}]),
            ToolMessage(content="y" * 30000, tool_call_id="a", name="web_fetch")]  # fmt: skip
    assert ct.trim_messages(msgs)[1] == 0  # recent: kept whole
    msgs += [AIMessage(content="thinking"), HumanMessage(content="next"), AIMessage(content="ok"), HumanMessage(content="go")]
    out, saved = ct.trim_messages(msgs)
    assert len(out[2].content) < 4200 and "inspect_result can reread it" in out[2].content and saved > 25000


def test_small_or_non_text_results_are_left_alone():
    msgs = [ToolMessage(content="short", tool_call_id="a", name="browser_act"), ToolMessage(content="also short", tool_call_id="b", name="browser_act"),
            ToolMessage(content=[{"type": "text", "text": "x" * 9000}], tool_call_id="c", name="browser_snapshot"),
            ToolMessage(content="latest", tool_call_id="d", name="browser_act")]  # fmt: skip
    out, saved = ct.trim_messages(msgs)
    assert saved == 0 and out == msgs


async def test_the_middleware_trims_what_the_model_sees():
    mw = ct.middleware()
    seen = {}

    class Request:
        def __init__(self, messages):
            self.messages = messages

        def override(self, **kw):
            return Request(kw["messages"])

    async def handler(request):
        seen["chars"] = sum(len(m.content) for m in request.messages if isinstance(m.content, str))
        return "response"

    msgs = _run(8)
    assert await mw.awrap_model_call(Request(msgs), handler) == "response"
    assert seen["chars"] < sum(len(m.content) for m in msgs if isinstance(m.content, str)) / 5


def test_the_agent_uses_it():
    import inspect

    from app.agent import deep

    assert "context_trim.middleware()" in inspect.getsource(deep.build_agent)
