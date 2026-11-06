"""Picking a date in any calendar (docs/BROWSING.md "Pickers"). Generic: no site names."""

from __future__ import annotations

import datetime as dt

from app.agent import fast_browser as fb
from app.connectors import computer_daemon as d

JAN18 = dt.date(2027, 1, 18)


def test_calendar_headings_are_read():
    assert d.caption_month("January 2027") == (2027, 1)
    assert d.caption_month("Jan '27") == (2027, 1)
    assert d.caption_month("Sept 2026") == (2026, 9)
    assert d.caption_month("Departure") is None and d.caption_month("Mon Jan 18 2027") is None


def test_a_labelled_cell_is_clicked():
    data = {"cells": [{"text": "Mon Jan 18 2027", "x": 5, "y": 6}], "grids": [], "next": {"x": 1, "y": 1}}
    assert d.plan_calendar(data, JAN18) == ("click", {"text": "Mon Jan 18 2027", "x": 5, "y": 6})


def test_a_month_grid_is_read_by_heading_and_day_number():
    grid = {"caption": "January 2027", "days": [{"d": 17, "x": 1, "y": 1}, {"d": 18, "x": 2, "y": 2}]}
    assert d.plan_calendar({"cells": [], "grids": [grid]}, JAN18) == ("click", {"d": 18, "x": 2, "y": 2})
    blocked = {"caption": "January 2027", "days": [{"d": 18, "x": 2, "y": 2, "disabled": True}]}
    assert d.plan_calendar({"cells": [], "grids": [blocked]}, JAN18) is None  # unavailable day: stop, don't guess


def test_the_month_arrows_move_towards_the_date():
    nxt, prv = {"x": 9, "y": 9}, {"x": 0, "y": 0}
    earlier = {"cells": [], "grids": [{"caption": "November 2026", "days": []}], "next": nxt, "prev": prv}
    later = {"cells": [], "grids": [{"caption": "March 2027", "days": []}], "next": nxt, "prev": prv}
    assert d.plan_calendar(earlier, JAN18) == ("next", nxt)
    assert d.plan_calendar(later, JAN18) == ("prev", prv)
    labelled_only = {"cells": [{"text": "Fri Nov 20 2026", "x": 1, "y": 1}], "grids": [], "next": nxt}
    assert d.plan_calendar(labelled_only, JAN18) == ("next", nxt)


def test_no_calendar_on_screen_never_clicks_an_arrow():
    assert d.plan_calendar({"cells": [{"text": "Next slide", "x": 1, "y": 1}], "grids": [], "next": {"x": 9, "y": 9}}, JAN18) is None


def test_the_snapshot_lists_calendar_days():
    for part in ('[role="gridcell"]', "[data-date]", "td[title]", "[aria-label][tabindex]"):
        assert part in d._SNAPSHOT_JS


def test_pick_date_is_a_step_the_fast_loop_and_recordings_know():
    assert "pick_date" in fb.ALLOWED_KINDS and "pick_date" in fb.FAST_SYSTEM
    plan = fb.parse_plan('{"actions": [{"kind": "pick_date", "ref": "e4", "text": "2027-01-18"}], "done": false}')
    assert plan["actions"] == [{"kind": "pick_date", "ref": "e4", "text": "2027-01-18"}]
    steps = fb.trace_steps([{"kind": "pick_date", "ref": "e4", "text": "2027-01-18"}], [{"i": 0, "target": {"label": "Departure"}}], {})
    assert steps[0]["text"] == "2027-01-18"
    made = fb.make_variables(steps)
    assert made[0]["param"] == "departure" and made[0]["example"] == "2027-01-18"


def test_the_agent_is_told_to_use_pick_date():
    import inspect

    from app.agent import deep, harness_extra

    assert "pick_date" in inspect.getsource(deep) and "pick_date" in inspect.getsource(harness_extra)
