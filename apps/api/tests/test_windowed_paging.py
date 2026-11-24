"""A tool that filters inside fixed windows (docs/LLD.md "Paged tables"): offset/limit step through the whole table,
only each window's matches come back, so pages are short or empty while hasMore is true. Every window is read."""

from __future__ import annotations

from app.agent import tables

TABLE = [{"article": f"A{i}", "store": "MP_X" if i % 7 == 0 else "OTHER", "qty": 1 + i % 5} for i in range(1430)]


def _window(offset: int, limit: int, search: str = "MP_X") -> dict:
    window = TABLE[offset:offset + limit]
    hits = [r for r in window if search.lower() in r["store"].lower()]
    return {"ok": True, "totalRows": len(hits), "returnedRows": len(hits), "offset": offset, "limit": limit,
            "hasMore": offset + limit < len(TABLE), "data": hits, "search": search.lower()}  # fmt: skip


def test_a_short_or_empty_page_that_says_more_is_a_window():
    assert tables.windowed(_window(0, 3))  # no matches in the window, hasMore true
    assert tables.windowed(_window(400, 500))
    assert not tables.windowed({"data": [1, 2, 3], "limit": 3, "hasMore": True})  # a full page: ordinary paging
    assert not tables.windowed(_window(1400, 500))  # the last window


async def test_every_window_is_read_and_only_matches_kept(monkeypatch):
    monkeypatch.setattr(tables, "_limits", lambda: (200, 100_000, 500, 4))
    asked = []

    async def call(args):
        asked.append(args["offset"])
        return _window(args["offset"], args["limit"])

    out = await tables.all_pages(_window(0, 3), {"key": "artDaily", "search": "MP_X"}, call)
    want = [r for r in TABLE if r["store"] == "MP_X"]
    assert out["data"] == want and out["windowed"] and out["hasMore"] is False
    assert out["totalRows"] == len(want)  # a real count now, not one window's
    assert sorted(asked)[0] == 3 and all(o % 500 == 3 for o in asked)  # contiguous windows after the first


async def test_a_tool_that_caps_windows_lower_is_followed(monkeypatch):
    monkeypatch.setattr(tables, "_limits", lambda: (200, 100_000, 5000, 3))

    async def call(args):
        return _window(args["offset"], min(args["limit"], 500))  # caps at 500 and says so in limit

    out = await tables.all_pages(_window(0, 500), {}, call)
    assert out["data"] == [r for r in TABLE if r["store"] == "MP_X"]
