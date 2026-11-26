"""Reply times for the admin console (docs/ADMIN.md "Reply times").

Every chat reply stores where its time went in ``meta.timing`` (app/chat/routes.py): the whole reply (``reply_ms``,
from the request arriving), the agent's stages, model time and calls, and each connector call with its first answer and
its other pages. This adds them up: how many replies took longer than ``REPLY_TARGET_S`` (45 seconds by default), the
median and 95th percentile, and the slowest replies with what took their time. Turns that were stopped or failed
keep their timing so far, marked as such. Message text is never included.
"""

from __future__ import annotations

from typing import Any


def _pct(values: list[int], p: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(p / 100 * (len(ordered) - 1))))]


def breakdown(timing: dict) -> dict:
    """One reply's time, split into what can be read at a glance: model, connector calls, other tools, the rest."""
    total = int(timing.get("reply_ms") or timing.get("total_ms") or 0)
    llm = int(timing.get("llm_ms") or 0)
    calls = [c for c in timing.get("calls") or [] if isinstance(c, dict)]
    conn = sum(int(c.get("ms") or 0) for c in calls)
    tools = sum(int(t.get("ms") or 0) for t in timing.get("tools") or [] if isinstance(t, dict) and t.get("tool") != "call_tool")
    slow_calls = sorted(calls, key=lambda c: -int(c.get("ms") or 0))[:3]
    by_tool: dict[str, list[int]] = {}
    for t in timing.get("tools") or []:
        if isinstance(t, dict) and t.get("tool"):
            agg = by_tool.setdefault(str(t["tool"]), [0, 0])
            agg[0] += 1
            agg[1] += int(t.get("ms") or 0)
    stages = {k: int(v) for k, v in (timing.get("stages") or {}).items() if isinstance(v, (int, float))}
    return {
        "total_ms": total,
        "model_ms": llm,
        "model_calls": int(timing.get("llm_calls") or 0),
        "connector_ms": conn,
        "tool_ms": tools,
        # Before each connector call: checking its schema, the gateway action, approval (can call the gateway itself).
        "prep_ms": sum(int(c.get("prep_ms") or 0) for c in calls),
        "first_token_ms": timing.get("first_token_ms"),
        "stages": stages,
        "slowest_calls": [{k: c.get(k) for k in ("connector", "tool", "ms", "first_ms", "prep_ms", "pages", "cached") if c.get(k) is not None}
                          for c in slow_calls],  # fmt: skip
        # Each tool the agent called: how many times and for how long in all, slowest first.
        "tools": [{"tool": k, "calls": v[0], "ms": v[1]} for k, v in sorted(by_tool.items(), key=lambda kv: -kv[1][1])][:8],
        # What neither the model, the connectors nor the tools account for (the app's own work between them).
        "other_ms": max(0, total - llm - conn - tools),
        # Time in the chat route before and after the agent ran (checks, history, saving the reply).
        "route_ms": max(0, total - int(timing.get("total_ms") or total)),
        # Each connector call in order: the action, its time, and what came back (rows, cut-off flags).
        "calls": [{k: c.get(k) for k in ("connector", "tool", "action", "ms", "prep_ms", "pages", "windows", "cached", "first_page_reused", "shape",
                                         "cut_for_model")
                   if c.get(k) is not None} for c in calls[:30]],  # fmt: skip
        # Remembered actions offered at the start of the turn (agent/action_memory.py).
        "remembered": [str(a) for a in timing.get("remembered") or []],
        "computer_used": bool(timing.get("computer_used")),
        # Scripts run on the computer this reply: each is a model step plus the run, one after another.
        "script_runs": by_tool.get("run_code", [0, 0])[0],
        "kind": timing.get("kind") or "",
        # A turn that didn't finish: stopped (by the person, or the reason the app stopped it) or failed.
        "ended": "failed" if timing.get("failed") else (f"stopped ({timing['stopped']})" if timing.get("stopped") else ""),
    }


def summarize(rows: list[tuple[Any, Any, dict]], target_s: float, slowest: int = 20) -> dict:
    """``rows``: (created_at, user email, meta.timing) per reply. Totals and the slowest replies."""
    items = []
    for at, email, timing in rows:
        if not isinstance(timing, dict):
            continue
        b = breakdown(timing)
        if b["total_ms"] <= 0:
            continue
        items.append({"at": at.isoformat() if hasattr(at, "isoformat") else at, "user": email or "", **b})
    totals = [i["total_ms"] for i in items]
    target_ms = int(target_s * 1000)
    over = [i for i in items if i["total_ms"] > target_ms]
    return {
        "target_s": target_s,
        "replies": len(items),
        "over_target": len(over),
        "p50_ms": _pct(totals, 50),
        "p95_ms": _pct(totals, 95),
        "max_ms": max(totals) if totals else None,
        # Of the replies over the target, where their time went in all: which part to make faster first.
        "over_target_split": {k: sum(i[k] for i in over) for k in ("model_ms", "connector_ms", "tool_ms")},
        "slowest": sorted(items, key=lambda i: -i["total_ms"])[:slowest],
    }
