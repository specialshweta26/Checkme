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
# Which agent is doing the work (set by run_deep_agent). Its use is also counted per agent for budgets (app/budgets).
AGENT: ContextVar = ContextVar("usage_agent", default=None)
FEATURES = ("chat", "inbox", "memory", "jobs", "other")
BACKGROUND = ("inbox", "memory")
FLUSH_EVERY_S = 10
BUDGET_CACHE_S = 60

# (user_id, day, feature, model) -> [calls, input, cached, output, units]
_pending: dict[tuple, list[float]] = defaultdict(lambda: [0, 0, 0, 0, 0.0])
_budget_cache: dict = {}
# (agent_id, user_id, day, model) -> [calls, input, cached, output, units]
_agent_pending: dict[tuple, list[float]] = defaultdict(lambda: [0, 0, 0, 0, 0.0])


def _today() -> date:
    return datetime.now(timezone.utc).date()


@contextmanager
def as_agent(agent_id):
    """Count the use inside this block against ``agent_id`` too (nested blocks win, so a teammate's turn is its own)."""
    token = AGENT.set(agent_id) if agent_id else None
    try:
        yield
    finally:
        if token is not None:
            AGENT.reset(token)


def _agent_row(user_id, model: str) -> list[float] | None:
    agent_id = AGENT.get()
    if not agent_id or not user_id:
        return None
    return _agent_pending[(agent_id, user_id, _today(), str(model or "unknown")[:80])]


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
    arow = _agent_row(meter[0], model)
    if arow is not None:
        arow[0] += 1
        arow[1] += int(input_tokens or 0)
        arow[2] += int(cached_tokens or 0)
        arow[3] += int(output_tokens or 0)


def count(kind: str, units: float = 1.0, user_id=None, feature: str | None = None) -> None:
    """A non-token resource: kind is "search" or "computer:starts" / "computer:minutes"."""
    meter = METER.get()
    uid = user_id or (meter[0] if meter else None)
    if not uid:
        return
    row = _pending[(uid, _today(), feature or (meter[1] if meter else "other"), kind[:80])]
    row[0] += 1
    row[4] += float(units)
    arow = _agent_row(uid, kind)
    if arow is not None:
        arow[0] += 1
        arow[4] += float(units)


def pending_for(user_id, day: date | None = None) -> list[tuple]:
    day = day or _today()
    return [(k, v) for k, v in list(_pending.items()) if k[0] == user_id and k[1] == day]


async def flush(session_factory=None) -> int:
    """Write the buffered counters. Safe to call any time; returns how many rows were upserted."""
    agents = await _flush_agents(session_factory)
    if not _pending:
        return agents
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
        return len(batch) + agents
    except Exception:  # noqa: BLE001 - put the counts back for the next try rather than lose them
        log.warning("usage flush failed; will retry", exc_info=True)
        for key, vals in batch.items():
            row = _pending[key]
            for i, v in enumerate(vals):
                row[i] += v
        return agents


async def _flush_agents(session_factory=None) -> int:
    if not _agent_pending:
        return 0
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.models import AgentUsageDaily

    if session_factory is None:
        from app.db import SessionLocal as session_factory  # noqa: N813
    batch = dict(_agent_pending)
    _agent_pending.clear()
    try:
        async with session_factory() as db:
            for (aid, uid, day, model), (calls, inp, cached, out, units) in batch.items():
                stmt = pg_insert(AgentUsageDaily).values(agent_id=aid, user_id=uid, day=day, model=model, calls=int(calls),
                                                         input_tokens=int(inp), cached_tokens=int(cached),
                                                         output_tokens=int(out), units=float(units))  # fmt: skip
                await db.execute(stmt.on_conflict_do_update(constraint="uq_agent_usage_daily", set_={
                    "calls": AgentUsageDaily.calls + stmt.excluded.calls,
                    "input_tokens": AgentUsageDaily.input_tokens + stmt.excluded.input_tokens,
                    "cached_tokens": AgentUsageDaily.cached_tokens + stmt.excluded.cached_tokens,
                    "output_tokens": AgentUsageDaily.output_tokens + stmt.excluded.output_tokens,
                    "units": AgentUsageDaily.units + stmt.excluded.units,
                }))  # fmt: skip
            await db.commit()
        return len(batch)
    except Exception:  # noqa: BLE001
        log.warning("agent usage flush failed; will retry", exc_info=True)
        for key, vals in batch.items():
            row = _agent_pending[key]
            for i, v in enumerate(vals):
                row[i] += v
        return 0


async def agent_usage(db, agent_id, since: date) -> dict:
    """This agent's spend since ``since`` (stored plus not yet written): priced cost, tokens, and whether any use was unpriced."""
    from sqlalchemy import select

    from app.models import AgentUsageDaily

    merged: dict[tuple, list[float]] = defaultdict(lambda: [0, 0, 0, 0, 0.0])
    for r in (await db.execute(select(AgentUsageDaily).where(AgentUsageDaily.agent_id == agent_id, AgentUsageDaily.day >= since))).scalars():
        row = merged[(r.day, r.model)]
        for i, v in enumerate((r.calls, r.input_tokens, r.cached_tokens, r.output_tokens, r.units)):
            row[i] += v or 0
    for (aid, _uid, day, model), vals in list(_agent_pending.items()):
        if str(aid) == str(agent_id) and day >= since:
            row = merged[(day, model)]
            for i, v in enumerate(vals):
                row[i] += v
    cost, tokens, unpriced, today_cost, today_tokens = 0.0, 0, False, 0.0, 0
    for (day, model), (_calls, inp, cached, outp, _units) in merged.items():
        if ":" in model or model == "search":
            continue
        tokens += int(inp) + int(outp)
        if day == _today():
            today_tokens += int(inp) + int(outp)
        c = cost_of(model, int(inp), int(cached), int(outp))
        if c is None:
            unpriced = unpriced or bool(inp or outp)
            continue
        cost += c
        if day == _today():
            today_cost += c
    return {"cost_usd": round(cost, 4), "today_usd": round(today_cost, 4), "tokens": tokens, "today_tokens": today_tokens,
            "unpriced": unpriced}  # fmt: skip


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
    """True when today's background work for this user is past the daily budget, or the user is past the daily cap
    on all model use. Cached for a minute."""
    if user_cap() != (0.0, 0):
        from app.models import User

        if await over_user_cap(db, await db.get(User, user_id)):
            return True
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


# The per-user daily cap on all model use (docs/ROLLOUT.md "Daily cap"): chat, tasks and background work together.
_cap_cache: dict = {}


def user_cap() -> tuple[float, int]:
    from app.config import get_settings

    s = get_settings()
    return float(getattr(s, "daily_user_budget_usd", 0) or 0), int(getattr(s, "daily_user_token_budget", 0) or 0)


def cap_exempt(email: str) -> bool:
    from app.config import get_settings

    raw = getattr(get_settings(), "budget_exempt", "") or ""
    exempt = {x.strip().lower() for x in raw.replace(";", ",").split(",") if x.strip()}
    return (email or "").strip().lower() in exempt


async def day_spend(db, user_id) -> dict:
    """Today's model use for this user across every feature: cost (when priced) and tokens."""
    rows = [r for r in await usage_rows(db, user_id, _today()) if ":" not in r["model"] and r["model"] != "search"]
    usd_limit, token_limit = user_cap()
    return {
        "cost_usd": round(sum(r["cost_usd"] or 0 for r in rows), 4),
        "tokens": sum(r["input"] + r["output"] for r in rows),
        "limit_usd": usd_limit or None,
        "limit_tokens": token_limit or None,
    }


async def over_user_cap(db, user) -> bool:
    """True when this user is past today's cap on all model use. Cached for a minute; exempt emails never are."""
    usd_limit, token_limit = user_cap()
    if (not usd_limit and not token_limit) or user is None or cap_exempt(getattr(user, "email", "")):
        return False
    hit = _cap_cache.get(user.id)
    if hit and time.monotonic() - hit[0] < BUDGET_CACHE_S:
        return hit[1]
    spend = await day_spend(db, user.id)
    over = bool((usd_limit and spend["cost_usd"] >= usd_limit) or (token_limit and spend["tokens"] >= token_limit))
    _cap_cache[user.id] = (time.monotonic(), over)
    if over:
        log.info("daily cap reached for %s: %s", user.id, spend)
    return over


CAP_MESSAGE = "You've reached today's usage limit for Lumio. It resets at midnight UTC."
