"""Collect a multi-page listing in code and answer questions about it in code.

A model adding things up from memory across many pages can be confidently wrong: one ran through all ten pages of a
listing, got the page and item totals right, and still miscounted an author. So the pages are read by code and stored, and
counts and sums are worked out by code from what was stored, with a figure for each page so the answer can be checked.

Generic: it finds "next" controls by what they're called, and treats each line of page text as a record (a table row is one
line, with tabs between cells).
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any

log = logging.getLogger(__name__)

MAX_PAGES = 200
MAX_CHARS = 2_000_000
ROWS_RETURNED = 60
SAME_PAGE_TRIES = 3
MIN_OVERLAP = 3
JOIN_LINES = 12
SCROLL_WAIT_MS = 1500
MAX_ROUNDS = 300
LINE_KINDS = 8
MIN_KIND_LINES = 3
MIXED_SHARE = 0.6
_NEXT_WORDS = ("next", "next page", "next »", "next ›", "next →", "next >", "›", "»", ">", "→", "older", "more results")
_BACK_WORDS = ("previous", "prev", "previous page", "« previous", "‹ previous", "← previous", "← prev", "«", "‹", "←", "newer")
_MORE_WORDS = ("load more", "show more", "see more", "view more", "more")
_MONEY = re.compile(r"(?:[$₹€£]|\b(?:rs\.?|inr|usd|eur|gbp)\s?)\s*(-?\d[\d,]*(?:\.\d+)?)", re.I)
_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _clean_name(raw: Any) -> str:
    return " ".join(str(raw or "").split()).casefold()


def _bare_name(raw: Any) -> str:
    """The name without arrows and other marks, so "Next →" and "Next" are the same control."""
    return " ".join(re.sub(r"[^\w\s]", " ", _clean_name(raw)).split())


def names_match(name: Any, words: tuple[str, ...]) -> bool:
    bare = _bare_name(name)
    return _clean_name(name) in words or bool(bare and bare in {_bare_name(w) for w in words})


def find_control(elements: list, words: tuple[str, ...], wanted: str = "") -> dict | None:
    """The enabled control with one of these names, or None. Links and buttons only, so it can't land on a text field. A
    wanted name goes first, and the usual names still count if it isn't on the page."""
    for names in ((wanted,), words) if wanted else (words,):
        for row in elements or []:
            if not isinstance(row, dict) or row.get("disabled"):
                continue
            if str(row.get("role") or "") in {"textbox", "searchbox", "combobox", "checkbox", "radio"}:
                continue
            if names_match(row.get("name"), names):
                return row
    return None


def _lines(text: str) -> list[str]:
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def grow(so_far: list[str], page: dict) -> tuple[list[str], bool]:
    """A page that grows in place, read again. A long page comes back as its start and end, so the new end is stitched on
    where it overlaps what's already held. Identical lines are real records, so this never removes duplicates. Returns the
    lines, and False if part of the page may be missing."""
    lines = _lines(page.get("text"))
    if not page.get("text_omitted"):
        return lines, True
    tail = lines[lines.index("…") + 1 :] if "…" in lines else lines
    tail = tail[1:]  # the end section starts wherever the cut fell, usually partway through a line
    # Find where the new end starts in what's held, and take everything from there from the new read. That copes with a
    # footer that's always last. A few lines must match: one or two can match by chance when records repeat.
    for size in range(min(JOIN_LINES, len(tail)), MIN_OVERLAP - 1, -1):
        start = tail[:size]
        for at in range(len(so_far) - size, -1, -1):
            if so_far[at : at + size] == start:
                return so_far[:at] + tail, True
    # Where the new end joins can't be found. Adding it could count things twice, and leaving it out could miss them.
    return so_far or lines, False


def _store(ctx: Any) -> dict:
    store = getattr(ctx, "_gathered", None)
    if store is None:
        store = {}
        ctx._gathered = store
    return store


async def _read(ctx: Any, call: Any) -> dict:
    page = await call(ctx, "browser_snapshot", {})
    return page if isinstance(page, dict) else {"ok": False, "error": str(page)[:200]}


async def _rewind(ctx: Any, call: Any, page: dict) -> tuple[dict, int]:
    """Back to the first page, so an earlier click can't leave the first pages out of a count."""
    moved = 0
    for _ in range(MAX_PAGES):
        back = find_control(page.get("elements") or [], _BACK_WORDS)
        if back is None:
            break
        after = await call(ctx, "browser_act", {"actions": [{"kind": "click", "ref": back.get("ref")}]})
        if not isinstance(after, dict) or after.get("ok") is False or str(after.get("text") or "") == str(page.get("text") or ""):
            break
        page = after
        moved += 1
    return page, moved


async def gather(ctx: Any, how: str = "auto", next_label: str = "", max_pages: int = 50, from_start: bool = True) -> dict:
    """Walk the listing and keep each page's text, from its first page unless from_start is False. how: next, load_more,
    scroll or auto."""
    from app.agent.computer_harness import run_computer as call

    ctx._gathering = True
    try:
        return await _gather(ctx, call, how, next_label, max_pages, from_start)
    finally:
        ctx._gathering = False


async def _gather(ctx: Any, call: Any, how: str, next_label: str, max_pages: int, from_start: bool) -> dict:
    limit = max(1, min(int(max_pages or 50), MAX_PAGES))
    rounds = 0
    how = str(how or "auto").lower()
    if how not in {"auto", "next", "load_more", "scroll"}:
        return {"ok": False, "error": "how must be auto, next, load_more or scroll"}
    pages: list[dict] = []
    cut: list[int] = []
    stopped = ""
    page = await _read(ctx, call)
    if page.get("ok") is False:
        return {"ok": False, "error": str(page.get("error") or "couldn't read the page")[:300]}
    rewound = 0
    if from_start and how in {"auto", "next"}:
        page, rewound = await _rewind(ctx, call, page)
    used = how
    grown: list[str] = []
    while True:
        text = str(page.get("text") or "")
        url = str(page.get("url") or "")
        if used in {"load_more", "scroll"}:
            grown, whole = grow(grown, page)
            if not whole and 1 not in cut:
                cut.append(1)
            pages = [{"url": url, "lines": grown}]
        else:
            if page.get("text_omitted"):
                cut.append(len(pages) + 1)
            pages.append({"url": url, "lines": _lines(text)})
        if used not in {"load_more", "scroll"} and len(pages) >= limit:
            stopped = f"reached the limit of {limit} pages"
            break
        elements = page.get("elements") or []
        control = None
        if used in {"auto", "next"}:
            control = find_control(elements, _NEXT_WORDS, next_label)
            if control is not None:
                used = "next"
        if control is None and used in {"auto", "load_more"}:
            control = find_control(elements, _MORE_WORDS, next_label)
            if control is not None:
                used = "load_more"
                grown = _lines(text)
                pages = [{"url": url, "lines": grown}]
        if control is None and used in {"auto", "scroll"} and used != "scroll":
            used = "scroll"
            grown = _lines(text)
            pages = [{"url": url, "lines": grown}]
        if control is None and used != "scroll":
            stopped = f"no {('Next' if used == 'next' else 'Load more')} control on the last page read, so this is the end"
            break
        # Slow pages take a moment to fill, so give them a few chances before calling this the end.
        steps = [{"kind": "click", "ref": control.get("ref")}] if control is not None else [{"kind": "scroll", "dy": 3000}, {"kind": "wait", "ms": SCROLL_WAIT_MS}]
        after = await call(ctx, "browser_act", {"actions": steps})
        tries = 0
        while isinstance(after, dict) and after.get("ok") is not False and str(after.get("text") or "") == text and tries < SAME_PAGE_TRIES:
            tries += 1
            # Each try waits longer: some pages pause between loads. When scrolling, go up a little and back down, so a
            # page that loads on reaching the bottom notices the bottom again.
            wait = {"kind": "wait", "ms": SCROLL_WAIT_MS * (tries + 1)}
            again = [{"kind": "scroll", "dy": -600}, {"kind": "scroll", "dy": 4000}, wait] if control is None else [wait]
            after = await call(ctx, "browser_act", {"actions": again})
        if not isinstance(after, dict) or after.get("ok") is False:
            stopped = f"stopped: {str((after or {}).get('error') if isinstance(after, dict) else after)[:200]}"
            break
        rounds += 1
        if str(after.get("text") or "") == text:
            stopped = "the page stopped changing, so this is the end"
            break
        if rounds >= MAX_ROUNDS:
            stopped = f"stopped after {MAX_ROUNDS} rounds"
            break
        page = after
    store = _store(ctx)
    dataset = f"d{len(store) + 1}"
    kept: list[dict] = []
    room = MAX_CHARS
    for row in pages:
        lines: list[str] = []
        for line in row["lines"]:
            if room <= 0:
                break
            room -= len(line)
            lines.append(line)
        kept.append({"url": row["url"], "lines": lines})
    store[dataset] = kept
    if cut:
        _store(ctx).setdefault("_incomplete", set()).add(dataset)
    out = {
        "ok": True,
        "dataset": dataset,
        "pages": len(kept),
        "lines": sum(len(row["lines"]) for row in kept),
        "how": used,
        "stopped": stopped,
        "note": (
            "Use gathered_query on this dataset for every count, sum and list, and report its figures. Don't count by eye. "
            "To count records, use starts_with with a line_kinds entry that appears once per record: contains also matches "
            "words inside other text."
        ),
    }
    kinds = _line_kinds(kept)
    if kinds:
        out["line_kinds"] = kinds
    if rewound:
        out["went_back_pages"] = rewound
    if cut:
        out["incomplete"] = True
        out["warning"] = f"Part of page(s) {cut} couldn't be read, so any count or total from this dataset may be wrong. Tell the user it's incomplete instead of giving it as the answer."
    return out


_CURRENCY_START = re.compile(r"^([$₹€£¥]|rs\.?|inr|usd|eur|gbp)\s?-?\d", re.I)


def _first_word(line: str) -> str:
    return line.split(maxsplit=1)[0] if line.split() else ""


def _kind(line: str) -> str:
    """A line's first word, or its currency for a line that starts with an amount ("£51.77" is a "£" line)."""
    money = _CURRENCY_START.match(line)
    if money:
        return money.group(1)
    word = _first_word(line)
    return word if word[:1].isalpha() else ""


def _line_kinds(pages: list[dict]) -> list[dict]:
    """How lines commonly start, with how many do: a listing's records repeat the same labels, so this shows what to count.
    Prices count too, so a total can use starts_with with the currency."""
    counts = Counter(kind for row in pages for kind in map(_kind, row["lines"]) if kind)
    return [{"starts_with": word, "lines": n} for word, n in counts.most_common(LINE_KINDS) if n >= MIN_KIND_LINES]


def _starts(low: str, start: str) -> bool:
    """Starts with these whole words: "by" isn't the start of "bystander"."""
    if not low.startswith(start):
        return False
    rest = low[len(start) :]
    return not rest or not rest[0].isalnum() or not start[-1:].isalnum()


def _matches(line: str, wanted: list[str], unwanted: list[str], starts: list[str]) -> bool:
    low = line.casefold()
    if starts and not any(_starts(low, s) for s in starts):
        return False
    return all(w in low for w in wanted) and not any(w in low for w in unwanted)


def _mixed_note(hits: list[str]) -> str:
    """Most matches start the same way and a few don't: the few usually just contain the word somewhere."""
    word, most = (Counter(map(_first_word, hits)).most_common(1) or [("", 0)])[0]
    if len(hits) < MIN_KIND_LINES or most == len(hits) or most / len(hits) < MIXED_SHARE:
        return ""
    return (f"{most} of these {len(hits)} lines start with '{word}'. The other {len(hits) - most} only contain your word somewhere. "
            f"If you're counting records, count again with starts_with ['{word}'].")  # fmt: skip


def _amount(line: str, column: int | None) -> float | None:
    source = line
    if column is not None:
        cells = line.split("\t")
        if column < 1 or column > len(cells):
            return None
        source = cells[column - 1]
    hits = _MONEY.findall(source)
    if len(hits) > 1:
        return None  # several amounts: which one is meant needs a column
    raw = hits[0] if hits else None
    if raw is None:
        numbers = _NUMBER.findall(source)
        if len(numbers) != 1:
            return None
        raw = numbers[0]
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def _as_list(raw: Any) -> list[str]:
    rows = raw if isinstance(raw, list) else [raw]
    return [str(w).casefold() for w in rows if str(w or "").strip()]


def query(ctx: Any, dataset: str, action: str = "count", contains: Any = "", exclude: Any = "", column: int | None = None,
          unique: bool = False, offset: int = 0, starts_with: Any = "") -> dict:  # fmt: skip
    """Count, sum or list the stored lines that contain every word in contains and none in exclude. If starts_with is given,
    a line must also start with one of those."""
    pages = _store(ctx).get(str(dataset))
    if pages is None:
        return {"ok": False, "error": f"No dataset {dataset} in this turn. Run browser_gather first."}
    if action not in {"count", "sum", "list"}:
        return {"ok": False, "error": "action must be count, sum or list"}
    wanted, unwanted, starts = _as_list(contains), _as_list(exclude), _as_list(starts_with)
    seen: set[str] = set()
    per_page: list[int] = []
    hits: list[str] = []
    for row in pages:
        found = 0
        for line in row["lines"]:
            if not _matches(line, wanted, unwanted, starts):
                continue
            if unique:
                if line in seen:
                    continue
                seen.add(line)
            found += 1
            hits.append(line)
        per_page.append(found)
    out: dict[str, Any] = {"ok": True, "dataset": dataset, "pages": len(pages), "matched_lines": len(hits), "per_page": per_page}
    if dataset in _store(ctx).get("_incomplete", ()):
        out["incomplete"] = True
        out["warning"] = "This dataset is incomplete, so this figure may be wrong. Tell the user that rather than giving it as the answer."
    mixed = _mixed_note(hits) if wanted and not starts else ""
    if mixed:
        out["warning"] = f"{out['warning']} {mixed}" if out.get("warning") else mixed
    if action == "sum":
        amounts = [_amount(line, column) for line in hits]
        good = [a for a in amounts if a is not None]
        out["sum"] = round(sum(good), 2)
        out["summed_lines"] = len(good)
        skipped = [line for line, a in zip(hits, amounts) if a is None]
        if skipped:
            out["skipped_lines"] = len(skipped)
            out["skipped_sample"] = [line[:120] for line in skipped[:5]]
            note = "Some matching lines had no single clear amount and aren't in the sum. Narrow contains, or give column."
            out["warning"] = f"{out['warning']} {note}" if out.get("warning") else note
    if action == "list":
        start = max(0, int(offset or 0))
        out["rows"] = [line[:300] for line in hits[start : start + ROWS_RETURNED]]
        out["more"] = len(hits) > start + ROWS_RETURNED
    else:
        out["sample"] = [line[:160] for line in hits[:5]]
    return out
