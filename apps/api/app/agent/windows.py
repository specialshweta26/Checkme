"""A source that cut its answer down for size, asked again in shorter date windows (docs/LLD.md "Shorter windows").

A gateway can answer a long period with "Response was too large for MCP and has been reduced. Narrow the window with
start/end". The agent then called the same tool with the same two weeks again and again (9 calls, 23 reads of the
reduced answer, 215 s) and still couldn't give every category. When an answer says it was reduced and the call has a
start and end date, Lumio splits the period in halves (and those halves again, while they are still reduced), asks
for every piece at once, and hands the agent each window's full answer to add up.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, timedelta
from typing import Any, Awaitable, Callable

REDUCED = re.compile(r"too large|has been reduced|been truncated|was truncated|narrow the (window|range|period|dates?)", re.I)
START_KEYS = ("start", "start_date", "since", "from", "from_date", "date_from", "begin", "startDate", "fromDate")
END_KEYS = ("end", "end_date", "until", "to", "to_date", "date_to", "endDate", "toDate")
MAX_WINDOWS = 16
MAX_DEPTH = 4
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def reduced(result: Any) -> bool:
    """The source says it cut this answer down for size."""
    if not isinstance(result, dict):
        return False
    for key in ("_note", "note", "message", "warning", "_warning"):
        value = result.get(key)
        if isinstance(value, str) and REDUCED.search(value):
            return True
    return False


def window(args: dict) -> tuple[str, str, date, date] | None:
    """(start key, end key, start, end) when the call names a period by plain dates (YYYY-MM-DD)."""
    start_key = next((k for k in START_KEYS if isinstance(args.get(k), str) and _DAY.match(args[k].strip())), None)
    end_key = next((k for k in END_KEYS if isinstance(args.get(k), str) and _DAY.match(args[k].strip())), None)
    if not start_key or not end_key:
        return None
    try:
        start, end = date.fromisoformat(args[start_key].strip()), date.fromisoformat(args[end_key].strip())
    except ValueError:
        return None
    return (start_key, end_key, start, end) if start < end else None


def halves(start: date, end: date) -> list[tuple[date, date]]:
    """Two windows covering every day once (both ends included)."""
    mid = start + timedelta(days=(end - start).days // 2)
    return [(start, mid), (mid + timedelta(days=1), end)]


async def split(call: Callable[[dict], Awaitable[Any]], args: dict, result: Any) -> Any:
    """``result`` again in shorter windows when the source reduced it; otherwise ``result`` as it was. ``call`` runs the
    same tool with other arguments."""
    found = window(args) if reduced(result) else None
    if found is None:
        return result
    start_key, end_key, start, end = found
    budget = {"left": MAX_WINDOWS}

    async def fetch(s: date, e: date, depth: int) -> list[dict]:
        budget["left"] -= 1
        try:
            got = await call({**args, start_key: s.isoformat(), end_key: e.isoformat()})
        except Exception as exc:  # noqa: BLE001 - one window failing is said, not hidden
            return [{"start": s.isoformat(), "end": e.isoformat(), "error": str(exc)[:300]}]
        if reduced(got) and e > s and depth < MAX_DEPTH and budget["left"] >= 2:
            parts = await asyncio.gather(*(fetch(a, b, depth + 1) for a, b in halves(s, e)))
            return [w for part in parts for w in part]
        row = {"start": s.isoformat(), "end": e.isoformat(), "data": got}
        if reduced(got):
            row["still_reduced"] = True
        return [row]

    parts = await asyncio.gather(*(fetch(a, b, 1) for a, b in halves(start, end)))
    windows = [w for part in parts for w in part]
    if all("error" in w for w in windows):
        return result
    return {
        "split_by_lumio": True,
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "windows": windows,
        "note": ("The source cut its answer for the whole period down as too large, so Lumio asked for it in "
                 f"{len(windows)} shorter windows that together cover every day once. Add the windows up for the whole "
                 "period (recompute averages and rates from the totals; don't add them up). A window marked still_reduced "
                 "or error is incomplete: say so."),
    }  # fmt: skip
