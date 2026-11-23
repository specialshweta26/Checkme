"""Figures in a reply must come from the data (docs/GUARDRAILS.md "Facts and figures").

After a turn that read connector data, every amount and count in the reply is checked against that data: a figure
passes when it matches a value in a tool result, a column total, or a sum, difference or ratio of figures that already
passed, within the precision it is written with ("₹66.77 crore" and "₹67 crore" both match 66,77,22,469). Percentages
and multiples ("11.4×") are commentary and are not checked; dates, years and day counts are skipped.

A reply with figures that don't match is rewritten once with the source data; anything still unmatched is marked in
the reply as not found in the source data, never left silently.
"""

from __future__ import annotations

import itertools
import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

MIN_FIGURE = 100  # smaller numbers are counts of days, ranks, list positions
MAX_SOURCE_NUMBERS = 50_000
MAX_CHECKED = 60
REL_TOL = 0.002

_SCALE = {
    "crore": 1e7, "crores": 1e7, "cr": 1e7, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5, "k": 1e3,
    "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6, "millions": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9,
    "billions": 1e9,
}  # fmt: skip
_NUM = re.compile(
    r"(?<![\w.\-/:])(?P<cur>[₹$€£]\s?)?(?P<num>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s?(?P<scale>crores?|cr|lakhs?|lacs?|thousand|millions?|mn|billions?|bn|[kKmMbB])\b)?"
    r"(?P<after>\s?(?:%|x\b|×|times\b|days?\b|weeks?\b|months?\b|years?\b|hours?\b|hrs?\b|minutes?\b|mins?\b|seconds?\b|"
    r"st\b|nd\b|rd\b|th\b|am\b|pm\b|[-/]\d))?",
    re.I,
)
_DATE_LIKE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|\b\d{1,2}:\d{2}\b")


def reply_figures(text: str) -> list[tuple[str, float, float]]:
    """(as written, value, half a unit of its last written digit) for each amount or count in the reply."""
    clean = _DATE_LIKE.sub(" ", text or "")
    clean = re.sub(r"`[^`]*`|https?://\S+|\(r\d+\)|\[[^\]]*\]\([^)]*\)", " ", clean)  # code, links, result ids
    out: list[tuple[str, float, float]] = []
    for m in _NUM.finditer(clean):
        if m.group("after"):
            continue  # a percentage, a multiple, a duration, a date part or an ordinal
        raw = m.group("num")
        digits = raw.replace(",", "")
        try:
            base = float(digits)
        except ValueError:
            continue
        scale = _SCALE.get((m.group("scale") or "").lower(), 1.0)
        if scale == 1.0 and not m.group("cur") and 1900 <= base <= 2100 and "." not in digits:
            continue  # a year
        value = base * scale
        if value < MIN_FIGURE:
            continue
        decimals = len(digits.split(".", 1)[1]) if "." in digits else 0
        half_unit = 0.5 * (10 ** -decimals) * scale
        out.append((m.group(0).strip(), value, half_unit))
        if len(out) >= MAX_CHECKED:
            break
    return out


def _numbers(value: Any, out: list[float], sums: dict, depth: int = 0) -> None:
    if depth > 14 or len(out) >= MAX_SOURCE_NUMBERS:
        return
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        out.append(float(value))
    elif isinstance(value, str):
        for m in re.finditer(r"-?\d[\d,]*(?:\.\d+)?", value[:20000]):
            try:
                out.append(float(m.group(0).replace(",", "")))
            except ValueError:
                pass
        if any(w in value[:20000].lower() for w in ("crore", "lakh", "lac", "million", "billion", " cr", "mn", "bn")):
            # A report's own text ("₹6.51 crore"): the figure as written, at its scale.
            out.extend(v for _, v, _ in reply_figures(value[:20000]))
    elif isinstance(value, dict):
        for v in value.values():
            _numbers(v, out, sums, depth + 1)
    elif isinstance(value, list):
        records = [v for v in value if isinstance(v, dict)]
        if len(records) >= 2:
            # Column totals of a list of records (the reply often totals a daily series), and totals per group of rows
            # (per date, per category, per date and category), also over rows nested in each record.
            _group_sums(_flat_rows(records), sums)
        for v in value:
            _numbers(v, out, sums, depth + 1)


MAX_FLAT_ROWS = 5_000
MAX_GROUPS = 300
MAX_SUMS = 60_000


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _flat_rows(records: list[dict]) -> list[dict]:
    """Each record's own text and number fields, and each row of a records list nested in it with the parent's text
    fields added (a day's ``by_category`` rows carry the day's ``date``)."""
    rows: list[dict] = []
    for rec in records:
        own = {k: v for k, v in rec.items() if isinstance(v, str) or _is_num(v)}
        rows.append(own)
        labels = {k: v for k, v in own.items() if isinstance(v, str)}
        for v in rec.values():
            if isinstance(v, list):
                for child in v:
                    if isinstance(child, dict):
                        rows.append({**labels, **{k: x for k, x in child.items() if isinstance(x, str) or _is_num(x)}})
        if len(rows) >= MAX_FLAT_ROWS:
            break
    return rows


def _group_sums(rows: list[dict], sums: dict) -> None:
    if not rows:
        return
    total = sum(len(v) for v in sums.values())
    numeric = sorted({k for r in rows for k, v in r.items() if _is_num(v)})
    labels = [k for k in sorted({k for r in rows for k, v in r.items() if isinstance(v, str)})
              if 1 < len({r.get(k) for r in rows if k in r}) <= MAX_GROUPS]  # fmt: skip
    groupings: list[tuple[str, ...]] = [()] + [(k,) for k in labels] + list(itertools.combinations(labels, 2))
    for keys in groupings:
        groups: dict = {}
        for r in rows:
            if any(k not in r for k in keys):
                continue
            g = groups.setdefault(tuple(r[k] for k in keys), {})
            for n in numeric:
                if _is_num(r.get(n)):
                    g[n] = g.get(n, 0.0) + float(r[n])
        if len(groups) > MAX_GROUPS * 4:
            continue
        for g in groups.values():
            for n, v in g.items():
                sums.setdefault(n, []).append(v)
                total += 1
                if total >= MAX_SUMS:
                    return


def source_numbers(results: list[dict]) -> list[float]:
    """Every number in this turn's tool results, plus each records list's column totals."""
    out: list[float] = []
    sums: dict = {}
    for row in results or []:
        _numbers(row.get("data") if isinstance(row, dict) else row, out, sums)
    for values in sums.values():
        out.extend(values)
    return out


def _close(value: float, half_unit: float, target: float) -> bool:
    return abs(value - target) <= max(half_unit, REL_TOL * abs(target), 0.5)


def unverified(text: str, results: list[dict]) -> list[str]:
    """The figures in ``text`` that neither the data nor a simple combination of verified figures supports."""
    figures = reply_figures(text)
    if not figures:
        return []
    sources = sorted({abs(n) for n in source_numbers(results) if abs(n) >= 1})
    import bisect

    def in_sources(value: float, half_unit: float) -> bool:
        tol = max(half_unit, REL_TOL * value, 0.5)
        i = bisect.bisect_left(sources, value - tol)
        return i < len(sources) and sources[i] <= value + tol

    verified: list[float] = []
    pending: list[tuple[str, float, float]] = []
    for written, value, half in figures:
        if in_sources(value, half):
            verified.append(value)
        else:
            pending.append((written, value, half))
    still: list[str] = []
    for written, value, half in pending:
        derived = False
        for a, b in itertools.permutations(verified[:40], 2):
            if any(_close(value, half, t) for t in (a + b, abs(a - b), a / b if b else -1)):
                derived = True
                break
        if derived:
            verified.append(value)
        elif written not in still:
            still.append(written)
    return still


def has_data(results: list[dict]) -> bool:
    """The turn read data worth checking against: at least a few numbers from a connector."""
    nums = source_numbers(results)
    return len(nums) >= 3


REPAIR_PROMPT = (
    "You wrote a reply for the user from tool data. These figures in it are not in the data and don't follow from it "
    "by a simple sum, difference or ratio: {figures}. Rewrite the reply so every figure comes from the data below: "
    "replace each with the exact value from the data (or rounded from it, with the unit kept); a figure that is a "
    "correct total of several data values may stay. Never leave a blank, a dash or an empty table cell where a figure "
    "was: if the data really doesn't have it, remove that row or column (or the table) and say in one line what the "
    "data doesn't cover. Keep everything else: structure, wording, tables, links. Reply with the rewritten reply "
    "only.\n\n"
    "Reply:\n{reply}\n\nData (this turn's tool results):\n{data}"
)


PROMPT_DATA_CHARS = 60_000


def _data_for_prompt(results: list[dict], limit: int = PROMPT_DATA_CHARS) -> str:
    rows = [{"source": r.get("source_tool"), "args": r.get("args"), "data": r.get("data")} for r in results or [] if isinstance(r, dict)]
    text = json.dumps(rows, default=str, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


def _fits_prompt(results: list[dict]) -> bool:
    """All of the data fits the rewrite prompt. When it doesn't, the rewriter sees only the start of it and took figures
    it couldn't see for missing: a report's totals were replaced with "not in the supplied data excerpt"."""
    from app.agent.jsonsize import fits

    rows = [{"source": r.get("source_tool"), "args": r.get("args"), "data": r.get("data")} for r in results or [] if isinstance(r, dict)]
    return fits(rows, PROMPT_DATA_CHARS)


_BLANK_CELL = re.compile(r"\|[ \t]*(?:-|—|–|n/?a)?[ \t]*(?=\|)", re.I)


def blank_cells(text: str) -> int:
    """Empty cells in the reply's markdown tables (separator rows aside)."""
    n = 0
    for line in (text or "").splitlines():
        row = line.strip()
        if not row.startswith("|") or re.fullmatch(r"\|?[\s:|-]+\|?", row):
            continue
        n += len(_BLANK_CELL.findall(row))
    return n


def flag(text: str, figures: list[str]) -> str:
    return (
        text.rstrip()
        + "\n\n> **Check these figures:** "
        + ", ".join(figures)
        + " — not found in the source data for this answer, so treat them as unverified."
    )


async def verify_reply(text: str, results: list[dict], complete=None) -> tuple[str, dict]:
    """(reply to send, report). Rewrites once when figures don't match the data; marks any left."""
    report: dict = {"checked": 0, "rewritten": False, "unverified": []}
    if not text or not has_data(results):
        return text, report
    report["checked"] = len(reply_figures(text))
    wrong = unverified(text, results)
    if not wrong:
        return text, report
    if not _fits_prompt(results):
        # Too much data to show the rewriter whole: keep the reply's figures and mark the unmatched ones instead.
        report["unverified"] = wrong
        report["too_much_data"] = True
        return flag(text, wrong), report
    if complete is None:
        from app.llm import chat_complete as complete
    try:
        resp = await complete(
            [{"role": "user", "content": REPAIR_PROMPT.format(figures=", ".join(wrong), reply=text, data=_data_for_prompt(results))}],
            max_tokens=4000,
        )
        fixed = (resp.choices[0].message.content or "").strip()
    except Exception:  # noqa: BLE001 - without the rewrite the figures are marked instead
        log.info("fact check rewrite failed", exc_info=True)
        fixed = ""
    if fixed and blank_cells(fixed) > blank_cells(text):
        log.info("fact check rewrite blanked table cells: kept the reply and marked its figures")
        fixed = ""
    if fixed:
        report["rewritten"] = True
        text = fixed
        wrong = unverified(text, results)
    report["unverified"] = wrong
    return (flag(text, wrong) if wrong else text), report
