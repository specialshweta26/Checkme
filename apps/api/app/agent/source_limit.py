"""A data source that says its own usage limit is used up (docs/LLD.md "Source limits").

A gateway answered "Organization daily data volume limit exceeded" and the agent called it again for every question
(two calls each, with different actions), getting the same refusal. That limit belongs to the source's account, not to
Lumio: nothing Lumio sends gets past it until it resets. When a result or error says so, Lumio remembers it for the
user and connector and answers further calls itself, without calling the source, with a plain note the agent passes
on. The pause lasts until the user's midnight (a daily limit) or an hour, whichever comes first; after that one call
is let through to see whether it reset. Never raises: a Redis error just calls the source.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

LIMIT = re.compile(
    r"(daily|monthly|hourly|organi[sz]ation|account|plan|usage)[^.\n]{0,40}\b(limit|quota)\b[^.\n]{0,20}\b(exceeded|reached|exhausted)"
    r"|\bquota (exceeded|exhausted)\b|data volume limit|(limit|quota) (has been |was )?(exceeded|reached) for (today|this (day|month))",
    re.I,
)
MAX_PAUSE = timedelta(hours=1)
MIN_PAUSE = timedelta(minutes=5)


def said(result: Any) -> str | None:
    """The source's own words when ``result`` (a result, an error dict or an exception) says its limit is used up."""
    if isinstance(result, BaseException):
        text = str(result)
    elif isinstance(result, dict):
        parts = [result.get(k) for k in ("error", "message", "detail", "_note", "note", "warning")]
        content = result.get("content")
        if isinstance(content, list):
            parts += [c.get("text") for c in content if isinstance(c, dict)]
        text = " ".join(p for p in parts if isinstance(p, str))
    elif isinstance(result, str):
        text = result
    else:
        return None
    found = LIMIT.search(text or "")
    if not found:
        return None
    start = max(0, found.start() - 60)
    return text[start : found.end() + 60].strip()[:240]


def _key(user_id, slug: str) -> str:
    return f"srclimit:{user_id}:{slug}"


def pause_until(tz_name: str | None, now: datetime | None = None) -> datetime:
    """The user's next midnight, or an hour from now if that's sooner (at least five minutes)."""
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
    current = (now or datetime.now(tz)).astimezone(tz)
    midnight = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(min(midnight, current + MAX_PAUSE), current + MIN_PAUSE)


async def mark(user, slug: str, words: str) -> dict:
    """Remember that ``slug`` refused for its limit; returns what to tell the agent."""
    until = pause_until(getattr(user, "timezone", None))
    note = {"source_said": words, "until": until.isoformat()}
    try:
        from app.cache import get_redis

        seconds = max(60, int((until - datetime.now(until.tzinfo)).total_seconds()))
        await get_redis().set(_key(user.id, slug), json.dumps(note), ex=seconds)
    except Exception:  # noqa: BLE001
        pass
    log.warning("source limit reached: %s (paused until %s)", slug, until.isoformat())
    return answer(slug, note)


async def paused(user, slug: str) -> dict | None:
    """What to answer instead of calling ``slug``, while it is paused."""
    try:
        from app.cache import get_redis

        raw = await get_redis().get(_key(user.id, slug))
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        return answer(slug, json.loads(raw))
    except ValueError:
        return None


def answer(slug: str, note: dict) -> dict:
    until = note.get("until") or ""
    try:
        when = datetime.fromisoformat(until).strftime("%H:%M")
    except ValueError:
        when = "later"
    return {
        "error": f"The data source's own usage limit is used up: it said \"{note.get('source_said', '')}\".",
        "source_limit": True,
        "retry_after": until,
        "_lumio_note": (
            f"This limit belongs to the source's account ({slug}), not to Lumio or the model, and every action on this "
            f"connector gets the same refusal. Lumio won't call it again until {when}. Don't try other actions on it "
            "now. Tell the user plainly: the source's daily data allowance is used up, it resets on the source's "
            "schedule (usually daily), and whoever manages that account can raise it; offer to try again later."
        ),
    }
