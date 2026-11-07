"""Tokens and cost for one chat turn (docs/LLD.md "Turn usage").

Every model call in a turn adds to the turn's tally: LangChain calls (the main agent) through TimingCallback, direct
OpenAI calls (fast browser loop, finish check, routing, embeddings) through app.llm. Nested runs (a teammate, a
healing step) add to the outer turn. Cost comes from MODEL_PRICES, a JSON map of model name to
[input, output, cached input] US dollars per million tokens; a model without a price still shows its tokens.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from typing import Any

log = logging.getLogger(__name__)

TURN: ContextVar[dict | None] = ContextVar("turn_usage", default=None)


def start() -> tuple[dict, Any | None]:
    """Begin a tally for this turn unless one is already running (a nested run adds to the outer one)."""
    current = TURN.get()
    if current is not None:
        return current, None
    tally: dict = {}
    return tally, TURN.set(tally)


def end(token: Any | None) -> None:
    if token is not None:
        TURN.reset(token)


def add(model: str, input_tokens: int = 0, output_tokens: int = 0, cached_tokens: int = 0) -> None:
    if input_tokens or output_tokens:
        from app.agent.meter import record_tokens

        record_tokens(model, input_tokens, output_tokens, cached_tokens)  # the per-user daily meter
    tally = TURN.get()
    if tally is None or not (input_tokens or output_tokens):
        return
    row = tally.setdefault(str(model or "unknown"), {"input": 0, "output": 0, "cached": 0, "calls": 0})
    row["input"] += int(input_tokens or 0)
    row["output"] += int(output_tokens or 0)
    row["cached"] += int(cached_tokens or 0)
    row["calls"] += 1


def _num(obj: Any, *names: str) -> int:
    for name in names:
        value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        if isinstance(value, (int, float)):
            return int(value)
    return 0


def from_openai(model: str, usage: Any) -> None:
    """usage from an OpenAI Responses / Embeddings answer."""
    if usage is None:
        return
    details = usage.get("input_tokens_details") if isinstance(usage, dict) else getattr(usage, "input_tokens_details", None)
    add(model, _num(usage, "input_tokens", "prompt_tokens"), _num(usage, "output_tokens", "completion_tokens"),
        _num(details, "cached_tokens") if details is not None else 0)  # fmt: skip


def from_langchain(response: Any) -> None:
    """usage from a LangChain LLMResult (usage_metadata on the message, else llm_output.token_usage)."""
    try:
        llm_output = getattr(response, "llm_output", None) or {}
        model = str(llm_output.get("model_name") or "")
        seen = False
        for gens in getattr(response, "generations", None) or []:
            for gen in gens or []:
                msg = getattr(gen, "message", None)
                meta = getattr(msg, "usage_metadata", None)
                if not meta:
                    continue
                resp_meta = getattr(msg, "response_metadata", None) or {}
                name = str(resp_meta.get("model_name") or resp_meta.get("model") or model)
                details = meta.get("input_token_details") or {}
                add(name, int(meta.get("input_tokens") or 0), int(meta.get("output_tokens") or 0), int(details.get("cache_read") or 0))
                seen = True
        if not seen and llm_output.get("token_usage"):
            from_openai(model, llm_output["token_usage"])
    except Exception:  # noqa: BLE001 - usage is informational
        log.debug("usage not read", exc_info=True)


def prices() -> dict[str, list[float]]:
    from app.config import get_settings

    raw = str(getattr(get_settings(), "model_prices", "") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("MODEL_PRICES is not valid JSON")
        return {}
    return {str(k): [float(x) for x in v] for k, v in data.items() if isinstance(v, (list, tuple)) and len(v) >= 2}


def _price_for(model: str, table: dict[str, list[float]]) -> list[float] | None:
    if model in table:
        return table[model]
    # "gpt-5.4-nano-2026-08-01" uses the "gpt-5.4-nano" price.
    hits = [k for k in table if model.startswith(k)]
    return table[max(hits, key=len)] if hits else None


def summary(tally: dict | None, table: dict[str, list[float]] | None = None) -> dict:
    """{"input", "output", "cached", "cost_usd" (None if no model is priced), "unpriced", "models"}."""
    tally = tally or {}
    table = prices() if table is None else table
    total_in = sum(r["input"] for r in tally.values())
    total_out = sum(r["output"] for r in tally.values())
    total_cached = sum(r["cached"] for r in tally.values())
    cost, priced, unpriced, models = 0.0, False, [], []
    for name, row in sorted(tally.items()):
        price = _price_for(name, table)
        line = {"model": name, **row}
        if price is not None:
            cached_rate = price[2] if len(price) > 2 else price[0]
            uncached = max(0, row["input"] - row["cached"])
            line["cost_usd"] = round((uncached * price[0] + row["cached"] * cached_rate + row["output"] * price[1]) / 1_000_000, 6)
            cost += line["cost_usd"]
            priced = True
        else:
            unpriced.append(name)
        models.append(line)
    return {"input": total_in, "output": total_out, "cached": total_cached,
            "cost_usd": round(cost, 6) if priced else None, "unpriced": unpriced, "models": models}  # fmt: skip
