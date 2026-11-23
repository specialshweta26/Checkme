"""A source that reduced its answer for size is asked again in shorter date windows (app/agent/windows.py). The
category question got "Response was too large for MCP and has been reduced. Narrow the window with start/end" and the
agent repeated the same two-week call 9 times."""

from __future__ import annotations

from datetime import date

from app.agent import windows

NOTE = "Response was too large for MCP and has been reduced. Narrow the window with start/end, or use a more specific tool."


def _days(s: str, e: str) -> int:
    return (date.fromisoformat(e) - date.fromisoformat(s)).days + 1


async def test_a_reduced_answer_is_asked_again_in_halves_until_it_fits():
    asked = []

    async def call(args):
        asked.append((args["start"], args["end"]))
        if _days(args["start"], args["end"]) > 4:
            return {"_note": NOTE, "summary": {}}
        return {"summary": {"by_category": {"Shoes": {"units": _days(args["start"], args["end"])}}}}

    args = {"brand": "one8", "start": "2026-11-09", "end": "2026-11-22"}
    out = await windows.split(call, args, {"_note": NOTE, "summary": {}})
    assert out["split_by_lumio"] and out["period"] == {"start": "2026-11-09", "end": "2026-11-22"}
    days = [w["data"]["summary"]["by_category"]["Shoes"]["units"] for w in out["windows"]]
    assert sum(days) == 14  # every day exactly once
    assert out["windows"][0]["start"] == "2026-11-09" and out["windows"][-1]["end"] == "2026-11-22"
    assert all("still_reduced" not in w for w in out["windows"]) and all(a[0] >= "2026-11-09" for a in asked)


async def test_answers_that_fit_or_have_no_dates_are_left_alone():
    async def call(args):
        raise AssertionError("not called")

    whole = {"summary": {"total": 1}}
    assert await windows.split(call, {"start": "2026-11-01", "end": "2026-11-09"}, whole) is whole
    cut = {"_note": NOTE}
    assert await windows.split(call, {"days": 14}, cut) is cut  # no window to split
    assert await windows.split(call, {"start": "2026-11-09", "end": "2026-11-09"}, cut) is cut  # one day


async def test_the_split_stops_at_a_budget_and_says_what_is_still_cut():
    async def call(args):
        return {"_note": NOTE}

    out = await windows.split(call, {"start": "2026-01-01", "end": "2026-12-31"}, {"_note": NOTE})
    assert len(out["windows"]) <= windows.MAX_WINDOWS and all(w.get("still_reduced") for w in out["windows"])
