"""A saved run used with new values, and fields that open a picker (docs/BROWSING.md "Variables", "Pickers").
Generic: no site names."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from app.agent import fast_browser as fb
from app.connectors import computer_daemon as d

RECORDED = [  # an old recording: plain typed text, and clicks on the suggestion and the calendar day it picked
    {"kind": "fill", "target": {"role": "textbox", "label": "From", "name_attr": "fromCity"}, "text": "Bengaluru"},
    {"kind": "click", "target": {"role": "option", "name": "Bengaluru, India BLR"}},
    {"kind": "fill", "target": {"role": "textbox", "label": "To", "name_attr": "toCity"}, "text": "Deoghar"},
    {"kind": "click", "target": {"role": "option", "name": "Deoghar, India DGH"}},
    {"kind": "click", "target": {"role": "gridcell", "aria_label": "Sat Dec 19 2026", "name": "19"}},
    {"kind": "click", "target": {"role": "button", "name": "Search"}},
]  # fmt: skip


def test_old_recordings_get_variables_and_clicks_on_old_values_become_ai_steps():
    steps = fb.make_variables(RECORDED)
    assert [s.get("param") for s in steps[:3:2]] == ["from", "to"]
    adapted, changed = fb.adapt_to_values(steps, {"from": "Chennai", "to": "Bangkok"})
    assert changed
    kinds = [s["kind"] for s in adapted]
    assert kinds == ["fill", "goal", "fill", "goal", "click", "click"]
    assert adapted[1]["text"].startswith("Choose “Chennai” for From") and "“Bengaluru”" in adapted[1]["text"]
    assert adapted[3]["text"].startswith("Choose “Bangkok” for To")
    assert adapted[5] == RECORDED[5]  # Search stays exact


def test_a_recorded_calendar_day_becomes_an_ai_step_when_the_date_changes():
    steps = [{"kind": "fill", "target": {"label": "Departure"}, "param": "departure", "example": "2026-12-19", "label": "Departure"},
             {"kind": "click", "target": {"role": "gridcell", "aria_label": "Sat Dec 19 2026"}}]  # fmt: skip
    adapted, _ = fb.adapt_to_values(steps, {"departure": "2026-12-23"})
    assert adapted[1]["kind"] == "goal" and "“2026-12-23”" in adapted[1]["text"]
    same, changed = fb.adapt_to_values(steps, {"departure": "2026-12-19"})
    assert same == steps and changed is False


def test_a_start_address_with_an_old_search_is_opened_bare():
    assert fb.bare_start("https://travel.test/flight/search?itinerary=BLR-DGH-19/12/2026&tripType=O") == "https://travel.test/flight/search"
    assert fb.bare_start("https://travel.test/flights/") == "https://travel.test/flights/"


async def test_replay_with_new_values_opens_the_bare_page_and_picks_new_values(monkeypatch):
    opened, sent, goals = [], [], []

    async def nav(c, url):
        opened.append(url)
        return {"ok": True, "url": url}

    async def internal(c, name, args):
        sent.append([s.get("text") or s.get("target", {}).get("name") for s in args["steps"]])
        return {"ok": True, "completed": len(args["steps"])}

    async def goal_step(ctx, trace, step, values, allow_final=False):
        goals.append(step["text"])
        return {"ok": True, "state": {}}

    monkeypatch.setattr(fb, "_navigate", nav)
    monkeypatch.setattr(fb, "_internal", internal)
    monkeypatch.setattr(fb, "run_goal_step", goal_step)
    trace = {"id": "t", "start_url": "https://travel.test/flight/search?itinerary=BLR-DGH", "steps": fb.make_variables(RECORDED)}
    out = await fb.replay(SimpleNamespace(), trace, {"from": "Chennai", "to": "Bangkok"})
    assert out["ok"] and opened == ["https://travel.test/flight/search"]
    assert sent[0] == ["Chennai"] and sent[1] == ["Bangkok"] and goals[0].startswith("Choose “Chennai”")
    opened.clear()
    await fb.replay(SimpleNamespace(), trace, {})  # same values as recorded: the page and clicks as they were
    assert opened == ["https://travel.test/flight/search?itinerary=BLR-DGH"]


def test_calendar_cells_are_matched_by_their_date_label():
    cells = [{"text": "Fri Dec 18 2026", "x": 1, "y": 1}, {"text": "Wed Dec 23 2026", "x": 9, "y": 9, "disabled": True},
             {"text": "2026-12-23 | Wednesday", "x": 5, "y": 5}]  # fmt: skip
    assert d.calendar_cell(cells, dt.date(2026, 12, 23)) == cells[2]
    assert d.calendar_cell(cells, dt.date(2027, 1, 1)) is None


def test_the_agent_is_told_how_to_handle_pickers_and_not_to_hand_over():
    import inspect

    from app.agent import deep

    src = inspect.getsource(deep)
    assert "opens a picker when clicked" in src and "Never hand the screen over because a form is awkward" in src
