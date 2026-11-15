"""Watches (docs/WATCHES.md): an agent wakes up when something it watches changes, not only on a clock.

- **Page watch** (``trigger_type="page"``): a public web page, checked every N minutes by a plain HTTP fetch, no model
  call. The first check only records the page. After that the agent wakes when the page changes, or, with a phrase to
  look for (``condition``), when that phrase newly appears. It gets the lines that were added and removed.
- **Inbox watch** (``trigger_type="inbox"``): wakes the agent when more than N inbox cards are waiting, once per
  crossing: it re-arms when the count drops back to N or below.

Each wake-up is one job on the existing automation path (``service.queue_event``): it runs as the watch's agent, page
text is wrapped as untrusted data, and sensitive actions ask first. Pages are fetched from the server, so every
address is checked to be public before each fetch and after redirects.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.automations import service
from app.models import Automation, InboxItem

log = logging.getLogger(__name__)

CHECK_S = 60
MIN_EVERY, MAX_EVERY, DEFAULT_EVERY = 15, 24 * 60, 60
MAX_THRESHOLD = 1000
MAX_TEXT = 60_000
MAX_LINES = 40
WATCH_TYPES = ("page", "inbox")


def every(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_EVERY
    return max(MIN_EVERY, min(n, MAX_EVERY))


def threshold(value: Any) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("An inbox watch needs a number: how many waiting cards is too many.") from None
    if not 0 <= n <= MAX_THRESHOLD:
        raise ValueError(f"Pick a number from 0 to {MAX_THRESHOLD}.")
    return n


def normalize(text: str) -> list[str]:
    """The page as comparable lines: whitespace collapsed, empty lines dropped."""
    lines = (" ".join(line.split()) for line in str(text or "")[:MAX_TEXT].splitlines())
    return [line for line in lines if line]


def fingerprint(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def page_change(state: dict, text: str, phrase: str = "") -> tuple[bool, dict, dict]:
    """Decide whether a fresh copy of the page should wake the agent. Returns (fire, new state, change).

    The first copy is the baseline. Without a phrase, any added or removed line fires. With a phrase, only its new
    appearance fires (present now, absent in the last copy), so a page that already says it doesn't keep firing."""
    lines = normalize(text)
    digest = fingerprint(lines)
    new_state = {**state, "hash": digest, "lines": lines[-400:]}
    if not state.get("hash"):
        return False, new_state, {}
    if digest == state["hash"]:
        return False, new_state, {}
    old = list(state.get("lines") or [])
    diff = list(difflib.ndiff(old, lines))
    added = [d[2:] for d in diff if d.startswith("+ ")][:MAX_LINES]
    removed = [d[2:] for d in diff if d.startswith("- ")][: MAX_LINES // 2]
    change = {"added": added, "removed": removed}
    if not added and not removed:
        return False, new_state, {}
    if phrase.strip():
        want = phrase.strip().lower()
        now_has = want in "\n".join(lines).lower()
        had = want in "\n".join(old).lower()
        return now_has and not had, new_state, change
    return True, new_state, change


def inbox_crossing(state: dict, count: int, limit: int) -> tuple[bool, dict]:
    """Fire once when the count goes above the limit; re-arm when it comes back down."""
    above = count > limit
    fire = above and not state.get("above")
    return fire, {**state, "above": above, "count": count}


async def _fetch(url: str) -> dict:
    """The page's text, refusing anything that isn't a public address (before fetching and after redirects)."""
    from app.connectors.urlsafety import validate_public_url
    from app.connectors.web_fetch import fetch_public_page

    await validate_public_url(url)
    page = await fetch_public_page(url)
    final = str(page.get("url") or url)
    if final != url:
        await validate_public_url(final)
    return page


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _due(row: Automation, now: datetime) -> bool:
    last = (row.state or {}).get("checked_at")
    if not last:
        return True
    try:
        return now - datetime.fromisoformat(last) >= timedelta(minutes=every(row.every_minutes))
    except ValueError:
        return True


def _prompt_page(row: Automation, page: dict, change: dict) -> str:
    look = row.condition.strip() if row.condition else ""
    data = {"url": row.trigger, "title": page.get("title") or "", "lines_added": change.get("added") or [], "lines_removed": change.get("removed") or []}
    return (
        f"Page watch: {row.name}\nWatching {row.trigger} every {every(row.every_minutes)} minutes.\n"
        + (f'It fired because "{look}" newly appeared on the page.\n' if look else "It fired because the page changed.\n")
        + f"What to do now: {row.action}\n"
        + service_event_data("page_change", data)
    )


def service_event_data(tag: str, data: dict) -> str:
    from app.automations.triggers import _event_data

    return _event_data(tag, data)


async def check_page(db: AsyncSession, row: Automation, fetch: Callable = _fetch) -> dict:
    """Check one page watch now: update its state and queue a job if it fired. Returns the new state."""
    state = dict(row.state or {})
    now = _now()
    try:
        page = await fetch(row.trigger)
    except Exception as exc:  # noqa: BLE001 - reported on the watch, retried next time
        state.update(checked_at=now.isoformat(), error=str(exc)[:300] or type(exc).__name__)
        row.state = state
        await db.commit()
        return state
    if page.get("block") or not page.get("ok", True) or not page.get("text"):
        state.update(checked_at=now.isoformat(), error="The site didn't return the page (it may block automated visits).")
        row.state = state
        await db.commit()
        return state
    fire, new_state, change = page_change(state, page.get("text") or "", row.condition or "")
    new_state.update(checked_at=now.isoformat(), error="", checks=int(state.get("checks") or 0) + 1, title=str(page.get("title") or "")[:200])
    if fire:
        new_state.update(changed_at=now.isoformat(), fired=int(state.get("fired") or 0) + 1,
                         last_change={"added": (change.get("added") or [])[:5], "removed": (change.get("removed") or [])[:3]})  # fmt: skip
    row.state = new_state
    await db.commit()
    if fire:
        await service.queue_event(db, row, f"page:{new_state['hash'][:32]}", f"page changed: {row.trigger}"[:120], _prompt_page(row, page, change))
    return new_state


async def check_inbox(db: AsyncSession, row: Automation) -> dict:
    limit = threshold(row.trigger)
    count = (await db.execute(select(func.count()).select_from(InboxItem).where(InboxItem.user_id == row.user_id, InboxItem.status == "pending"))).scalar_one()
    fire, new_state = inbox_crossing(dict(row.state or {}), int(count), limit)
    now = _now()
    new_state["checked_at"] = now.isoformat()
    if fire:
        new_state.update(changed_at=now.isoformat(), fired=int((row.state or {}).get("fired") or 0) + 1)
    row.state = new_state
    await db.commit()
    if fire:
        senders = (
            await db.execute(
                select(InboxItem.sender, InboxItem.subject).where(InboxItem.user_id == row.user_id, InboxItem.status == "pending")
                .order_by(InboxItem.created_at.desc()).limit(15)
            )
        ).all()  # fmt: skip
        data = {"waiting_cards": int(count), "limit": limit, "latest": [{"from": s, "subject": t} for s, t in senders]}
        prompt = (
            f"Inbox watch: {row.name}\nIt fired because {count} inbox cards are waiting (more than {limit}).\n"
            f"What to do now: {row.action}\n" + service_event_data("inbox_state", data)
        )
        await service.queue_event(db, row, f"inbox:{now.strftime('%Y%m%d%H%M')}", f"{count} cards waiting", prompt)
    return new_state


async def check_all(session_factory: Callable, now: datetime | None = None) -> int:
    """One pass over every enabled watch that is due. Returns how many fired."""
    now = now or _now()
    fired = 0
    async with session_factory() as db:
        ids = (
            await db.execute(select(Automation.id).where(Automation.trigger_type.in_(WATCH_TYPES), Automation.enabled.is_(True)))
        ).scalars().all()
    for automation_id in ids:
        try:
            async with session_factory() as db:
                row = await db.get(Automation, automation_id)
                if row is None or not row.enabled:
                    continue
                before = int((row.state or {}).get("fired") or 0)
                if row.trigger_type == "page":
                    if not _due(row, now):
                        continue
                    state = await check_page(db, row)
                else:
                    state = await check_inbox(db, row)
                fired += int(state.get("fired") or 0) > before
        except Exception:  # noqa: BLE001 - one watch must not stop the others
            log.exception("watch %s failed", automation_id)
    return fired


async def watch_loop(session_factory: Callable | None = None) -> None:
    if session_factory is None:
        from app.db import SessionLocal

        session_factory = SessionLocal
    from app.jobs.worker import wake

    await asyncio.sleep(50)
    while True:
        try:
            if await check_all(session_factory):
                wake()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("watch tick failed")
        await asyncio.sleep(CHECK_S)


async def start_watches() -> asyncio.Task:
    return asyncio.create_task(watch_loop(), name="watches")


def describe(row: Automation) -> dict:
    """What the app shows about a watch."""
    state = row.state or {}
    return {
        "every_minutes": every(row.every_minutes) if row.trigger_type == "page" else None,
        "checked_at": state.get("checked_at"),
        "changed_at": state.get("changed_at"),
        "fired": int(state.get("fired") or 0),
        "error": state.get("error") or "",
        "title": state.get("title") or "",
        "count": state.get("count"),
        "last_change": state.get("last_change") or None,
        "baseline": bool(state.get("hash")) if row.trigger_type == "page" else None,
    }


_URL = re.compile(r"^https?://", re.I)


def clean_url(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        raise ValueError("A page watch needs the page's address.")
    if not _URL.match(text):
        text = "https://" + text
    if len(text) > 2000 or " " in text:
        raise ValueError("That isn't a web address.")
    return text
