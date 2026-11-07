"""What each user costs: model tokens, searches and computer use, per user, per day, per feature.

Callers say whose work is running (``with metered(user_id, "inbox"):``); every model and embedding call inside adds to
that user's counters (app/agent/usage.py reports here), and searches and computer starts are counted with ``count``.
Counters are kept in memory and written to usage_daily every few seconds (one upsert per user/day/feature/model, not
one per call); several instances add up correctly because the upsert increments.

Background work (inbox triage, memory learning) has a daily budget per user (DAILY_BACKGROUND_BUDGET_USD, or
DAILY_BACKGROUND_TOKEN_BUDGET when models have no price); over it, background work falls back to what needs no model.
Chat is never blocked by this budget.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

METER: ContextVar[tuple | None] = ContextVar("usage_meter", default=None)
FEATURES = ("chat", "inbox", "memory", "jobs", "other")
BACKGROUND = ("inbox", "memory")
FLUSH_EVERY_S = 10
BUDGET_CACHE_S = 60

# (user_id, day, feature, model) -> [calls, input, cached, output, units]
_pending: dict[tuple, list[float]] = defaultdict(lambda: [0, 0, 0, 0, 0.0])
_budget_cache: dict = {}


def _today() -> date:
    return datetime.now(timezone.utc).date()


@contextmanager
def metered(user_id, feature: str):
    """Attribute the model calls, searches and computer use inside this block to ``user_id`` and ``feature``."""
    token = METER.set((user_id, feature if feature in FEATURES else "other")) if user_id else None
    try:
        yield
    finally:
        if token is not None:
            METER.reset(token)


def record_tokens(model: str, input_tokens: int, output_tokens: int, cached_tokens: int) -> None:
    meter = METER.get()
    if meter is None:
        return
    row = _pending[(meter[0], _today(), meter[1], str(model or "unknown")[:80])]
    row[0] += 1
    row[1] += int(input_tokens or 0)
    row[2] += int(cached_tokens or 0)
    row[3] += int(output_tokens or 0)


def count(kind: str, units: float = 1.0, user_id=None, feature: str | None = None) -> None:
    """A non-token resource: kind is "search" or "computer:starts" / "computer:minutes"."""
    meter = METER.get()
    uid = user_id or (meter[0] if meter else None)
    if not uid:
        return
    row = _pending[(uid, _today(), feature or (meter[1] if meter else "other"), kind[:80])]
    row[0] += 1
    row[4] += float(units)


def pending_for(user_id, day: date | None = None) -> list[tuple]:
    day = day or _today()
    return [(k, v) for k, v in list(_pending.items()) if k[0] == user_id and k[1] == day]


async def flush(session_factory=None) -> int:
    """Write the buffered counters. Safe to call any time; returns how many rows were upserted."""
    if not _pending:
        return 0
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.models import UsageDaily

    if session_factory is None:
        from app.db import SessionLocal as session_factory  # noqa: N813
    batch = dict(_pending)
    _pending.clear()
    try:
        async with session_factory() as db:
            for (uid, day, feature, model), (calls, inp, cached, out, units) in batch.items():
                stmt = pg_insert(UsageDaily).values(user_id=uid, day=day, feature=feature, model=model, calls=int(calls),
                                                    input_tokens=int(inp), cached_tokens=int(cached), output_tokens=int(out),
                                                    units=float(units))
                await db.execute(stmt.on_conflict_do_update(constraint="uq_usage_daily", set_={
                    "calls": UsageDaily.calls + stmt.excluded.calls,
                    "input_tokens": UsageDaily.input_tokens + stmt.excluded.input_tokens,
                    "cached_tokens": UsageDaily.cached_tokens + stmt.excluded.cached_tokens,
                    "output_tokens": UsageDaily.output_tokens + stmt.excluded.output_tokens,
                    "units": UsageDaily.units + stmt.excluded.units,
                }))
            await db.commit()
        return len(batch)
    except Exception:  # noqa: BLE001 - put the counts back for the next try rather than lose them
        log.warning("usage flush failed; will retry", exc_info=True)
        for key, vals in batch.items():
            row = _pending[key]
            for i, v in enumerate(vals):
                row[i] += v
        return 0


async def flush_loop() -> None:
    try:
        while True:
            await asyncio.sleep(FLUSH_EVERY_S)
            await flush()
    except asyncio.CancelledError:
        await flush()  # don't lose the last seconds on shutdown
        raise


async def start_usage_flusher() -> asyncio.Task:
    return asyncio.create_task(flush_loop(), name="usage-flush")


# --- reading and budgets -------------------------------------------------------------------------------------------


def cost_of(model: str, inp: int, cached: int, out: int, table: dict | None = None) -> float | None:
    from app.agent.usage import _price_for, prices

    price = _price_for(model, prices() if table is None else table)
    if price is None:
        return None
    cached_rate = price[2] if len(price) > 2 else price[0]
    return (max(0, inp - cached) * price[0] + cached * cached_rate + out * price[1]) / 1_000_000


async def usage_rows(db, user_id, since: date) -> list[dict]:
    """This user's counters since ``since`` (stored plus not yet written), one dict per day/feature/model."""
    from sqlalchemy import select

    from app.models import UsageDaily

    merged: dict[tuple, list[float]] = defaultdict(lambda: [0, 0, 0, 0, 0.0])
    for r in (await db.execute(select(UsageDaily).where(UsageDaily.user_id == user_id, UsageDaily.day >= since))).scalars():
        row = merged[(r.day, r.feature, r.model)]
        for i, v in enumerate((r.calls, r.input_tokens, r.cached_tokens, r.output_tokens, r.units)):
            row[i] += v or 0
    for (uid, day, feature, model), vals in list(_pending.items()):
        if uid == user_id and day >= since:
            row = merged[(day, feature, model)]
            for i, v in enumerate(vals):
                row[i] += v
    out = []
    for (day, feature, model), (calls, inp, cached, outp, units) in sorted(merged.items()):
        out.append({"day": day.isoformat(), "feature": feature, "model": model, "calls": int(calls), "input": int(inp),
                    "cached": int(cached), "output": int(outp), "units": round(float(units), 2),
                    "cost_usd": None if ":" in model or model == "search" else cost_of(model, int(inp), int(cached), int(outp))})
    return out


def budget() -> tuple[float, int]:
    from app.config import get_settings

    s = get_settings()
    return float(getattr(s, "daily_background_budget_usd", 0) or 0), int(getattr(s, "daily_background_token_budget", 0) or 0)


async def background_spend(db, user_id) -> dict:
    """Today's background use for this user: cost (when priced), tokens, and the limits."""
    rows = [r for r in await usage_rows(db, user_id, _today()) if r["feature"] in BACKGROUND]
    usd_limit, token_limit = budget()
    return {
        "cost_usd": round(sum(r["cost_usd"] or 0 for r in rows), 4),
        "tokens": sum(r["input"] + r["output"] for r in rows),
        "limit_usd": usd_limit or None,
        "limit_tokens": token_limit or None,
    }


async def over_budget(db, user_id) -> bool:
    """True when today's background work for this user is past the daily budget. Cached for a minute."""
    usd_limit, token_limit = budget()
    if not usd_limit and not token_limit:
        return False
    hit = _budget_cache.get(user_id)
    if hit and time.monotonic() - hit[0] < BUDGET_CACHE_S:
        return hit[1]
    spend = await background_spend(db, user_id)
    over = bool((usd_limit and spend["cost_usd"] >= usd_limit) or (token_limit and spend["tokens"] >= token_limit))
    _budget_cache[user_id] = (time.monotonic(), over)
    if over:
        log.info("background budget reached for %s: %s", user_id, spend)
    return over
