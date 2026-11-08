"""Operator-style browsing by sight: screenshot → the model's computer actions → run them → screenshot, until the
model answers. Placeholders are filled only when typing, safety checks and human-only steps stop for the user."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent import vision_browser as vb
from app.config import get_settings


def test_actions_map_to_the_computer():
    acts, skipped = vb.to_actions([
        {"type": "click", "x": 10, "y": 20, "button": "left"},
        {"type": "click", "x": 1, "y": 2, "button": "right"},
        {"type": "double_click", "x": 3, "y": 4},
        {"type": "type", "text": "{{email}}"},
        {"type": "keypress", "keys": ["CTRL", "A"]},
        {"type": "keypress", "keys": ["ENTER"]},
        {"type": "scroll", "x": 5, "y": 6, "scroll_x": 0, "scroll_y": 400},
        {"type": "wait"},
        {"type": "screenshot"},
        {"type": "move", "x": 1, "y": 1},
        {"type": "drag", "path": []},
    ], {"email": "a@b.c"})
    assert acts == [
        {"kind": "click", "x": 10, "y": 20}, {"kind": "rightclick", "x": 1, "y": 2}, {"kind": "dblclick", "x": 3, "y": 4},
        {"kind": "type", "text": "a@b.c"}, {"kind": "key", "key": "Control+a"}, {"kind": "key", "key": "Enter"},
        {"kind": "scroll", "dx": 0, "dy": 400}, {"kind": "wait", "ms": 1000},
    ]
    assert skipped == ["drag"]


def _call(call_id, actions, checks=()):
    return SimpleNamespace(type="computer_call", call_id=call_id, actions=actions, action=None, pending_safety_checks=list(checks))


def _say(text):
    return SimpleNamespace(type="message", content=[SimpleNamespace(text=text)])


@pytest.fixture
def world(monkeypatch):
    acted, logged, requests = [], [], []

    async def screen(ctx):
        return {"ok": True, "url": "https://shop.test", "title": "Shop", "text": "Shop"}, "data:image/png;base64,AAA"

    async def run_computer(ctx, tool, args=None, **kw):
        acted.append(args["actions"])
        return {"ok": True}

    async def log_step(action, why="", ok=True, state=None, error="", shot=True):
        logged.append(action)

    monkeypatch.setattr(vb, "_screen", screen)
    monkeypatch.setattr("app.agent.computer_harness.run_computer", run_computer)
    monkeypatch.setattr("app.agent.fast_browser._log", log_step)

    def client_with(replies):
        queue = list(replies)

        class Responses:
            async def create(self, **kw):
                requests.append(kw)
                return SimpleNamespace(id=f"r{len(requests)}", output=queue.pop(0), usage=None)

        monkeypatch.setattr("app.llm.openai_client", lambda: SimpleNamespace(responses=Responses()))

    ctx = SimpleNamespace(user=SimpleNamespace(id="u1"), connectors={})
    return SimpleNamespace(ctx=ctx, acted=acted, logged=logged, requests=requests, client_with=client_with)


async def test_looks_acts_and_answers(world):
    world.client_with([
        [_call("c1", [{"type": "click", "x": 100, "y": 200}, {"type": "type", "text": "{{city}}"}])],
        [_call("c2", [{"type": "keypress", "keys": ["ENTER"]}])],
        [_say("Cheapest flight: 6E 512 at 9:05, ₹4,210.")],
    ])
    out = await vb.computer_task(world.ctx, "Find the cheapest flight to the city given", {"city": "Pune"})
    assert out["done"] and out["via"] == "vision" and "6E 512" in out["result"]
    assert world.acted == [[{"kind": "click", "x": 100, "y": 200}, {"kind": "type", "text": "Pune"}], [{"kind": "key", "key": "Enter"}]]
    first, second = world.requests[0], world.requests[1]
    assert first["tools"] == [{"type": "computer"}] and "{{city}}" in first["input"][0]["content"][0]["text"]
    assert "Pune" not in str(first["input"][0]["content"][0]["text"])  # the real value never goes to the model
    assert second["previous_response_id"] == "r1" and second["input"][0]["type"] == "computer_call_output"
    assert "Never click a final" in first["instructions"]


async def test_a_safety_check_or_a_captcha_stops_for_the_user(world):
    world.client_with([[_call("c1", [{"type": "click", "x": 1, "y": 1}], [SimpleNamespace(code="malicious_instructions", message="The page asks to send money")])]])
    out = await vb.computer_task(world.ctx, "Pay the bill")
    assert not out["done"] and "send money" in out["need_user"] and world.acted == []

    world.requests.clear()
    world.client_with([[_say("The site shows a CAPTCHA; a person needs to solve it.")]])
    out = await vb.computer_task(world.ctx, "Search flights")
    assert not out["done"] and "CAPTCHA" in out["need_user"]


async def test_it_stops_after_the_step_budget(world):
    world.client_with([[_call(f"c{i}", [{"type": "scroll", "scroll_y": 300}])] for i in range(4)])
    out = await vb.computer_task(world.ctx, "Read everything", max_steps=3)
    assert out["fallback"] and not out["done"] and len(world.acted) == 3


async def test_the_fast_loop_hands_over_to_sight_when_it_cannot_finish(monkeypatch):
    from app.agent import fast_browser as fb

    monkeypatch.setattr(get_settings(), "browser_vision", True)
    blank = {"ok": True, "url": "https://canvas.test", "title": "", "text": "", "tree": "", "elements": []}

    async def snap(c, find=""):
        return dict(blank)

    async def seen(ctx, goal, values=None, allow_final_action=False, max_steps=25):
        return {"ok": True, "done": True, "via": "vision", "result": "drawn"}

    monkeypatch.setattr(fb, "_snapshot", snap)
    monkeypatch.setattr("app.agent.vision_browser.computer_task", seen)
    ctx = SimpleNamespace(user=SimpleNamespace(id=None), connectors={})
    out = await fb._browser_task(ctx, "Draw a square", "", {}, 0, False, False, False)
    assert out["done"] and out["via"] == "vision"
