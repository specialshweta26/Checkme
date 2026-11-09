"""Each chat gets its own tab, and screenshots go into the chat."""

from types import SimpleNamespace

from app.agent import browser_tabs as tabs
from app.agent import page_shots
from app.desktop import agent as desktop


def test_a_chat_keeps_its_own_tab():
    assert tabs.choose({"tab": "t2", "at": 1}, {"other": {"tab": "t1", "at": 1000}}, 1000) == ("t2", "mine")


def test_the_first_chat_takes_the_page_already_open():
    assert tabs.choose(None, {}, 1000) == ("", "claim")


def test_a_second_chat_at_the_same_time_gets_a_new_tab():
    assert tabs.choose(None, {"other": {"tab": "t1", "at": 990}}, 1000) == ("", "new")


def test_an_idle_tab_is_reused_before_opening_another():
    others = {"busy": {"tab": "t1", "at": 9990}, "idle": {"tab": "t2", "at": 100}}
    assert tabs.choose(None, others, 10000) == ("t2", "reuse")


def test_a_closed_tab_is_recognised():
    assert tabs.tab_gone({"ok": False, "error": "no tab t3; open tabs: t1"})
    assert not tabs.tab_gone({"ok": False, "error": "page failed"})
    assert not tabs.tab_gone({"ok": True})


async def test_calls_without_a_chat_or_with_their_own_tab_are_left_alone():
    async def call(tool, args):
        raise AssertionError("no browser call expected")

    no_chat = SimpleNamespace(session_id=None, user=SimpleNamespace(id=1))
    with_chat = SimpleNamespace(session_id="s1", user=SimpleNamespace(id=1))
    assert await tabs.tab_for(no_chat, "browser_act", {}, "cloud", call) is None
    assert await tabs.tab_for(with_chat, "browser_act", {"tab": "t9"}, "cloud", call) is None
    assert await tabs.tab_for(with_chat, "computer_observe", {}, "cloud", call) is None


def test_goals_asking_for_evidence_get_screenshots():
    assert page_shots.wants_shots("Capture screenshots of the saved record")
    assert page_shots.wants_shots("Screenshot evidence.")
    assert not page_shots.wants_shots("Sort products by price")


def test_screenshot_names_are_safe_and_ordered():
    assert page_shots.shot_name(2, "Order: Confirmed!/Done") == "screenshot-02-order-confirmed-done.png"
    assert page_shots.shot_name(1, "") == "screenshot-01-page.png"


def test_a_page_screenshot_is_kept_apart_from_the_live_panel():
    import base64

    out = desktop._keep_page_shot({"ok": True, "screenshot_b64": base64.b64encode(b"\x89PNG-data").decode()})
    assert "screenshot_b64" not in out
    assert desktop.take_page_shot(out["page_shot_key"]) == b"\x89PNG-data"
    assert desktop.take_page_shot(out["page_shot_key"]) == b""


def test_the_page_check_notices_a_reorder_of_the_same_text():
    from app.connectors.computer_daemon import _PAGE_SIG_JS

    assert "charCodeAt" in _PAGE_SIG_JS


async def test_a_stalled_model_call_is_retried_then_gives_up(monkeypatch):
    import asyncio

    import pytest

    from app.agent import context_trim as trim

    monkeypatch.setattr(trim, "MODEL_CALL_LIMIT_S", 0.01)
    calls = []

    async def stuck_once(request):
        calls.append(request)
        if len(calls) == 1:
            await asyncio.sleep(1)
        return "answer"

    assert await trim.call_with_limit(stuck_once, "req") == "answer"
    assert len(calls) == 2

    async def always_stuck(request):
        await asyncio.sleep(1)

    with pytest.raises(TimeoutError):
        await trim.call_with_limit(always_stuck, "req")
