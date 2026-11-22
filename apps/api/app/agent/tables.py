"""Whole tables from a connector (docs/LLD.md "Paged tables").

Some tools answer a big table a page at a time (``totalRows``, ``offset``, ``limit``, ``hasMore``) with rows as plain
arrays. Another MCP client read every page and added the rows up with code; Lumio read only the first page. Here:

- ``all_pages`` fetches the remaining pages of a paged answer and merges the rows;
- ``aggregate`` filters, groups and sums a stored table (rows as arrays or objects), so totals come from every row and
  never from the model adding up a sample. It is fixed arithmetic, not code the model writes.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

# Defaults; TABLE_MAX_PAGES, TABLE_MAX_ROWS, TABLE_PAGE_ROWS and TABLE_PAGE_CONCURRENCY override them.
MAX_PAGES = 200
MAX_ROWS = 100_000
PAGE_ROWS = 5000  # asked for on later pages; a tool that caps lower simply returns fewer
PAGE_CONCURRENCY = 6
_MORE = ("hasMore", "has_more", "more")
_TOTAL = ("totalRows", "total_rows", "total", "totalCount", "total_count", "count")
_LIST_KEYS = ("data", "rows", "items", "results", "records")


def _rows_key(page: dict) -> str | None:
    for key in _LIST_KEYS:
        if isinstance(page.get(key), list):
            return key
    lists = [k for k, v in page.items() if isinstance(v, list)]
    return lists[0] if len(lists) == 1 else None


def _num(page: dict, keys: tuple) -> int | None:
    for key in keys:
        value = page.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def is_paged(page: Any) -> bool:
    if not isinstance(page, dict) or _rows_key(page) is None:
        return False
    if any(page.get(k) is True for k in _MORE):
        return True
    total, offset = _num(page, _TOTAL), _num(page, ("offset",))
    rows = page.get(_rows_key(page)) or []
    return total is not None and offset is not None and offset + len(rows) < total


def _limits() -> tuple[int, int, int, int]:
    """(max pages, max rows, rows asked per page, pages fetched at once), from the settings."""
    try:
        from app.config import get_settings

        st = get_settings()
        return (int(getattr(st, "table_max_pages", MAX_PAGES) or MAX_PAGES), int(getattr(st, "table_max_rows", MAX_ROWS) or MAX_ROWS),
                int(getattr(st, "table_page_rows", PAGE_ROWS) or PAGE_ROWS),
                max(1, int(getattr(st, "table_page_concurrency", PAGE_CONCURRENCY) or 1)))  # fmt: skip
    except Exception:  # noqa: BLE001
        return MAX_PAGES, MAX_ROWS, PAGE_ROWS, PAGE_CONCURRENCY


async def all_pages(first: Any, args: dict, call: Callable[[dict], Awaitable[Any]]) -> Any:
    """``first`` with every remaining page's rows added. ``args`` are the arguments of the first call (the paging
    parameters are set on them for each later page); ``call(args)`` runs the tool. Stops at the page and row limits.

    The second page is read on its own: it shows how many rows the tool really returns per page. After that pages are
    read ``TABLE_PAGE_CONCURRENCY`` at a time and merged in order, with or without a total: a table that only says
    ``hasMore`` is read ahead, and reading stops at its first empty, short or last page (at most a few calls past
    the end are wasted). A short page in the middle moves the next batch to the right offset, and a page that fails
    is read again in order, so the merged table is the same as reading page by page."""
    if not is_paged(first):
        return first
    max_pages, max_rows, page_rows, concurrency = _limits()
    key = _rows_key(first)
    rows = list(first.get(key) or [])
    total = _num(first, _TOTAL)
    first_limit = _num(first, ("limit",))
    start = _num(first, ("offset",)) or 0
    limit = max(first_limit or len(rows) or 1, page_rows)
    offset = start + len(rows)
    pages = 1

    async def fetch(at: int, size: int) -> tuple[list | None, bool]:
        """(rows or None when the call failed, whether the page says more follow)."""
        nonlocal limit
        for attempt in (size, first_limit):
            if not attempt:
                continue
            try:
                page = await call({**args, "offset": at, "limit": attempt})
            except Exception:  # noqa: BLE001 - a tool that refuses the bigger page gets its own size
                if attempt != first_limit and first_limit:
                    limit = first_limit
                    continue
                log.info("page at offset %s failed", at, exc_info=True)
                return None, False
            got = page.get(key) if isinstance(page, dict) else None
            return (list(got) if got else []), is_paged(page)
        return None, False

    def room() -> bool:
        return pages < max_pages and len(rows) < max_rows and (total is None or offset < total)

    more = True
    step = 0
    if room():  # the second page, on its own
        got, more = await fetch(offset, limit)
        if got:
            rows.extend(got)
            offset += len(got)
            pages += 1
            step = len(got)
        else:
            more = False
    while more and step and room():
        n = min(concurrency, max_pages - pages)
        if total is not None:
            n = min(n, -(-(min(total, start + max_rows) - offset) // step))
        starts = [offset + i * step for i in range(max(1, n))]
        sem = asyncio.Semaphore(concurrency)

        async def one(at: int):
            async with sem:
                return await fetch(at, min(limit, step))

        batch = await asyncio.gather(*(one(at) for at in starts))
        for at, (got, page_more) in zip(starts, batch):
            if at != offset:
                break  # a short page before this one: the next batch starts at the right offset
            if got is None:
                got, page_more = await fetch(offset, min(limit, step))  # read again, in order
                if got is None:
                    more = False
                    break
            if not got:
                more = False
                break
            rows.extend(got)
            offset += len(got)
            pages += 1
            if not page_more or (total is None and len(got) < step):
                more = False
                break
            if not room():
                break
    # Stopped by the page or row limit while the table went on: say so, so the answer isn't taken as the whole table.
    cut = (more and step > 0 and (pages >= max_pages or len(rows) >= max_rows)) or (total is not None and len(rows) < total)
    rows = rows[:max_rows]
    merged = {**first, key: rows, "returnedRows": len(rows), "offset": start, "pages_fetched": pages}
    for flag in _MORE:
        if flag in merged:
            merged[flag] = bool(cut)
    if cut:
        merged["truncated"] = True
        merged["note"] = (f"Only the first {len(rows)} rows were read (the limit is TABLE_MAX_ROWS={max_rows} rows, "
                          f"TABLE_MAX_PAGES={max_pages} pages); the table has more. Narrow the request (dates, filters, a "
                          "grouped or summary tool) for complete totals, or say the figures cover only these rows.")  # fmt: skip
    return merged


# --- adding up a stored table ---------------------------------------------------------------------------------------


def find_rows(data: Any) -> list:
    """The biggest list of rows in a result."""
    best: list = []

    def walk(v: Any, depth: int = 0) -> None:
        nonlocal best
        if depth > 6:
            return
        if isinstance(v, list):
            if len(v) > len(best) and v and isinstance(v[0], (list, dict)):
                best = v
            for x in v[:5]:
                walk(x, depth + 1)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x, depth + 1)

    walk(data)
    return best


def _cell(row: Any, col: Any) -> Any:
    if isinstance(row, list):
        try:
            return row[int(col)]
        except (ValueError, TypeError, IndexError):
            return None
    if isinstance(row, dict):
        return row.get(str(col))
    return None


def _matches(value: Any, want: Any) -> bool:
    if isinstance(want, list):
        return any(_matches(value, w) for w in want)
    if isinstance(want, dict):
        if value is None:
            return False
        for op, bound in want.items():
            try:
                x, y = float(value), float(bound)
            except (TypeError, ValueError):
                x, y = str(value), str(bound)
            if (op == "gte" and not x >= y) or (op == "lte" and not x <= y) or (op == "gt" and not x > y) or (op == "lt" and not x < y):
                return False
        return True
    return str(value).strip().lower() == str(want).strip().lower()


def aggregate(data: Any, where: dict | None = None, group_by: list | None = None, sums: list | None = None, top: int = 50) -> dict:
    """Totals over every row: rows kept by ``where`` ({column: value, [values] or {"gte", "lte"}}), grouped by
    ``group_by`` columns, with ``sums`` columns added up and a row count. Columns are indexes for array rows, keys for
    object rows."""
    rows = find_rows(data)
    if not rows:
        return {"error": "No table of rows in this result."}
    kept = [r for r in rows if all(_matches(_cell(r, c), w) for c, w in (where or {}).items())]
    sums = list(sums or [])
    groups: dict = defaultdict(lambda: [0, [0.0] * len(sums)])
    for r in kept:
        key = tuple(_cell(r, c) for c in (group_by or []))
        g = groups[key]
        g[0] += 1
        for i, c in enumerate(sums):
            v = _cell(r, c)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                g[1][i] += v
    total = [round(sum(g[1][i] for g in groups.values()), 2) for i in range(len(sums))]
    out = [{"group": dict(zip([str(c) for c in (group_by or [])], k)), "rows": g[0],
            "sums": {str(c): round(g[1][i], 2) for i, c in enumerate(sums)}} for k, g in groups.items()]  # fmt: skip
    out.sort(key=lambda x: -(x["sums"][str(sums[0])] if sums else x["rows"]))
    return {"rows_in_table": len(rows), "rows_matched": len(kept), "totals": {str(c): total[i] for i, c in enumerate(sums)},
            "groups": out[: max(1, int(top or 50))], "groups_total": len(out),
            "sample_rows": rows[:3]}  # fmt: skip
