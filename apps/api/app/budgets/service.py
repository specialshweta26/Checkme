"""Per-agent budgets and limits (docs/BUDGETS.md).

Each agent may have, in ``workspace_agents.limits``:

- ``daily_usd``: what it may spend on models per day (UTC), across chat, group chats and background tasks.
- ``monthly_usd``: the same per calendar month.
- ``daily_tokens``: model tokens per day (UTC). Works without model prices (``MODEL_PRICES``), which dollar budgets need.
- ``task_minutes``: how long one run of a background task may go before it pauses and asks.

Spend is counted per agent by the meter (``app/agent/meter.py``: ``as_agent``) into ``agent_usage_daily``. Over a
budget:

- A chat turn gets a short reply saying so, and no model is called.
- A background task pauses before it starts (or mid-run, checked every ``WATCH_S``) with a question: run it anyway today
  / leave it. "Anyway" lets that task go past the budget for the rest of the day.

Past ``task_minutes``, the run stops and the task asks "keep going" (another run of the same length) or "stop".
Unpriced models (no price set) don't count toward a dollar budget.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone
from typing import Any

from app.agent.meter import agent_usage
from app.agent.usage import prices

WATCH_S = 30
CACHE_S = 20
MAX_USD = 10_000
MAX_MINUTES = 24 * 60
MAX_TOKENS = 1_000_000_000
BUDGET = "budget"
TIME_LIMIT = "time_limit"
RUN_ANYWAY = "Run it anyway today"
LEAVE_IT = "Leave it for now"
KEEP_GOING = "Keep going"
STOP_IT = "Stop it"

_cache: dict[str, tuple[float, dict]] = {}


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _num(value: Any, lo: float, hi: float, label: str, whole: bool = False) -> float | int | None:
    if value is None or value == "":
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number.") from None
    if n <= 0:
        return None  # 0 or less clears the limit
    if n < lo or n > hi:
        raise ValueError(f"{label} must be between {lo:g} and {hi:g}.")
    return int(round(n)) if whole else round(n, 2)


def clean_limits(body: dict) -> dict:
    out: dict[str, Any] = {}
    daily = _num(body.get("daily_usd"), 0.01, MAX_USD, "The daily budget")
    monthly = _num(body.get("monthly_usd"), 0.01, MAX_USD, "The monthly budget")
    tokens = _num(body.get("daily_tokens"), 1000, MAX_TOKENS, "The daily token limit", whole=True)
    minutes = _num(body.get("task_minutes"), 1, MAX_MINUTES, "The time limit", whole=True)
    if daily and monthly and daily > monthly:
        raise ValueError("The daily budget can't be more than the monthly one.")
    for key, val in (("daily_usd", daily), ("monthly_usd", monthly), ("daily_tokens", tokens), ("task_minutes", minutes)):
        if val is not None:
            out[key] = val
    return out


def limits(agent: Any) -> dict:
    raw = getattr(agent, "limits", None) or {}
    if not isinstance(raw, dict):
        return {}
    try:
        return clean_limits(raw)
    except ValueError:
        return {}


def forget(agent_id) -> None:
    _cache.pop(str(agent_id), None)


async def status(db, agent: Any, fresh: bool = False) -> dict:
    """Spend today and this month against the agent's limits. ``over`` is "daily", "monthly" or None."""
    key = str(getattr(agent, "id", ""))
    lim = limits(agent)
    hit = _cache.get(key)
    if hit and not fresh and time.monotonic() - hit[0] < CACHE_S and hit[1]["limits"] == lim:
        return hit[1]
    today = _today()
    use = await agent_usage(db, agent.id, today.replace(day=1))
    over = None
    if lim.get("daily_usd") and use["today_usd"] >= lim["daily_usd"]:
        over = "daily"
    elif lim.get("monthly_usd") and use["cost_usd"] >= lim["monthly_usd"]:
        over = "monthly"
    elif lim.get("daily_tokens") and use["today_tokens"] >= lim["daily_tokens"]:
        over = "tokens"
    out = {"limits": lim, "today_usd": use["today_usd"], "month_usd": use["cost_usd"], "today_tokens": use["today_tokens"],
           "tokens_month": use["tokens"], "unpriced": use["unpriced"], "priced": bool(prices()), "over": over}  # fmt: skip
    _cache[key] = (time.monotonic(), out)
    return out


def _money(n: float) -> str:
    return f"${n:,.2f}"


def over_text(agent: Any, st: dict) -> str:
    name = getattr(agent, "name", "") or "This agent"
    if st["over"] == "daily":
        return f"{name} has reached its daily budget of {_money(st['limits']['daily_usd'])} (spent {_money(st['today_usd'])} today)."
    if st["over"] == "tokens":
        return f"{name} has used its {st['limits']['daily_tokens']:,} tokens for today ({st['today_tokens']:,} so far)."
    return f"{name} has reached its monthly budget of {_money(st['limits']['monthly_usd'])} (spent {_money(st['month_usd'])} this month)."


def chat_reply(agent: Any, st: dict) -> str:
    name = getattr(agent, "name", "") or "this agent"
    when = "at the start of next month" if st["over"] == "monthly" else "tomorrow (UTC)"
    return f"{over_text(agent, st)} I won't run anything until it resets {when}. To keep going now, raise the budget in {name}'s About panel, under Limits."


def may_overspend(job: Any) -> bool:
    """The user said "run it anyway today" for this task."""
    return (getattr(job, "meta", None) or {}).get("budget_ok_day") == _today().isoformat()


def pause(job: Any, why: str, text: str) -> list:
    """Pause a background task with a question (budget or time limit). The approvals queue and the chat show it."""
    from app.jobs import service as jobs

    options = [RUN_ANYWAY, LEAVE_IT] if why == BUDGET else [KEEP_GOING, STOP_IT]
    meta = dict(job.meta or {})
    meta["question"] = {"question": text, "options": options, "waiting_for": why}
    job.meta = meta
    job.status = "waiting_input"
    job.result = text
    return [jobs.event(job, "question", text, {"options": options, "waiting_for": why, "status": "waiting_input"})]


def answer(job: Any, q: dict, text: str) -> list | None:
    """A reply to a budget / time-limit question. Returns events, or None when it isn't one of those questions."""
    from app.jobs import service as jobs

    why = str(q.get("waiting_for") or "")
    if why not in (BUDGET, TIME_LIMIT):
        return None
    low = text.strip().lower()
    go = text == (RUN_ANYWAY if why == BUDGET else KEEP_GOING) or low in {"yes", "y", "ok", "go", "go ahead", "keep going", "continue", "run it", "run it anyway"}
    meta = dict(job.meta or {})
    meta.pop("question", None)
    if not go:
        job.meta = meta
        return jobs.cancel(job)
    if why == BUDGET:
        meta["budget_ok_day"] = _today().isoformat()
    if meta.get("limit_paused"):
        meta.pop("limit_paused", None)
        meta["next_query"] = (
            "You were paused by a limit the user set and they said to carry on. Continue where you left off: check your "
            "checklist and what you already did, don't redo finished steps."
        )
    job.meta = meta
    job.status = "queued"
    job.finished_at = None
    label = "Running past the budget today" if why == BUDGET else "Keep going"
    return [jobs.event(job, "status", f"You said: {label}", {"status": "queued", "answer": text})]
