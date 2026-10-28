"""Flag conflicting facts across per-page research notes, always citing both sides.

Cheap pass: pull "<subject words> <number><unit>" claims out of each note and report the same subject with
materially different values on different pages. Optional LLM pass (when the app has a chat provider) catches
non-numeric conflicts; its output is kept only when it cites URLs that are really in the notes.
"""

from __future__ import annotations

import json
import re
from typing import Awaitable, Callable
from urllib.parse import urlsplit

_STOP = {
    "the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "is", "are", "was", "were", "be", "been", "it", "its",
    "with", "by", "at", "as", "that", "this", "from", "has", "have", "had", "about", "around", "approximately", "roughly",
    "nearly", "over", "under", "than", "more", "less", "up", "only", "just", "some", "per", "each", "which", "who",
    "will", "can", "may", "now", "also", "total", "totals", "estimated", "reported", "according",
}
_NUM = re.compile(
    r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(%|percent|million|billion|thousand|crore|lakh|km|kg|mb|gb|tb|ms|"
    r"seconds?|minutes?|hours?|days?|years?|meters?|metres?|miles?|people|users|employees|usd|inr|eur|\$|rs)?\b",
    re.I,
)
_SCALE = {"thousand": 1e3, "million": 1e6, "billion": 1e9, "lakh": 1e5, "crore": 1e7}
_WORD = re.compile(r"[a-z][a-z\-]{1,}")


def _claims(note: str) -> list[tuple[frozenset, str, float, str]]:
    out = []
    for sent in re.split(r"(?<=[.!?;])\s+|\n+", note or ""):
        for m in _NUM.finditer(sent):
            raw, unit = m.group(1), (m.group(2) or "").lower()
            try:
                val = float(raw.replace(",", ""))
            except ValueError:
                continue
            if unit in _SCALE:
                val, unit = val * _SCALE[unit], ""
            if not unit and 1900 <= val <= 2100 and "." not in raw:
                unit = "year"
            unit = {"percent": "%", "$": "usd", "rs": "inr"}.get(unit, unit.rstrip("s") if len(unit) > 3 else unit)
            before = [w for w in _WORD.findall(sent[: m.start()].lower()) if w not in _STOP][-4:]
            if len(before) < 2:
                continue
            out.append((frozenset(before), unit, val, sent.strip()[:240]))
    return out


def _site(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")


def find_contradictions(notes: list[dict], *, rel_tol: float = 0.05, limit: int = 8) -> list[dict]:
    """Heuristic numeric conflicts: same subject (>=2 shared words) and unit, values differ by more than ``rel_tol``."""
    claims = []
    for n in notes:
        for subj, unit, val, quote in _claims(n.get("note") or ""):
            claims.append({"subj": subj, "unit": unit, "value": val, "quote": quote, "url": n.get("url") or ""})
    found: list[dict] = []
    seen: set[tuple] = set()
    for i, a in enumerate(claims):
        for b in claims[i + 1 :]:
            if a["url"] == b["url"] or a["unit"] != b["unit"] or _site(a["url"]) == _site(b["url"]) and a["quote"] == b["quote"]:
                continue
            shared = a["subj"] & b["subj"]
            if len(shared) < 2:
                continue
            hi = max(abs(a["value"]), abs(b["value"])) or 1.0
            if abs(a["value"] - b["value"]) / hi <= rel_tol:
                continue
            key = (frozenset(shared), a["unit"], frozenset({a["url"], b["url"]}))
            if key in seen:
                continue
            seen.add(key)
            found.append(
                {
                    "about": " ".join(sorted(shared)) + (f" ({a['unit']})" if a["unit"] else ""),
                    "kind": "numeric",
                    "claims": [
                        {"value": _fmt(a["value"]), "url": a["url"], "quote": a["quote"]},
                        {"value": _fmt(b["value"]), "url": b["url"], "quote": b["quote"]},
                    ],
                }
            )
            if len(found) >= limit:
                return found
    return found


def _fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


async def llm_contradictions(notes: list[dict], goal: str, llm: Callable[[str], Awaitable[str]], limit: int = 6) -> list[dict]:
    """Ask the configured chat model for conflicts between notes; drop anything that does not cite two real URLs."""
    use = [n for n in notes if n.get("note")][:12]
    if len(use) < 2:
        return []
    body = "\n\n".join(f"[{n['url']}]\n{n['note'][:700]}" for n in use)
    prompt = (
        "These are notes from different web pages for one research goal. List facts where the pages disagree "
        "(different numbers, dates, names or yes/no answers about the same thing). Do not list differences that are "
        'just different topics. Reply as JSON {"conflicts": [{"about": str, "claims": [{"value": str, "url": str, '
        '"quote": str}]}]} with exactly the URLs shown, or {"conflicts": []}.\n'
        f"Goal: {goal}\n\n{body}"
    )
    try:
        raw = await llm(prompt)
        data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 - the heuristic result still stands
        return []
    urls = {n["url"] for n in use}
    out = []
    for c in data.get("conflicts") or []:
        claims = [x for x in (c.get("claims") or []) if isinstance(x, dict) and x.get("url") in urls]
        if len({x["url"] for x in claims}) >= 2 and c.get("about"):
            out.append({"about": str(c["about"])[:200], "kind": "llm", "claims": claims[:4]})
    return out[:limit]
