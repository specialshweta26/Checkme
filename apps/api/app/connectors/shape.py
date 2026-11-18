from __future__ import annotations

import json
import re
from calendar import monthrange
from datetime import date, timedelta
from typing import Any

from app.llm import chat_complete
from connector_core.calendar import now_local


def slim_search(result: Any, query: str = "") -> dict:
    if not isinstance(result, dict):
        return {"raw": result}
    question = query or str(result.get("query") or "")
    candidates = []
    for item in (result.get("candidates") or result.get("matches") or [])[:12]:
        if not isinstance(item, dict):
            continue
        fields, _required = _schema_fields(item)
        candidates.append(
            {
                "public_name": item.get("public_name") or item.get("name"),
                "description": (item.get("description") or "")[:800],
                "score": item.get("score"),
                "arguments": fields,
            }
        )
    return {"query": result.get("query") or question, "candidates": candidates}


def _entity_slug(value: str) -> str:
    return re.sub(r"[\s\-_]+", "", value).strip()


def entity_mentioned(entity: str, blob: str) -> bool:
    name = (entity or "").strip().lower()
    text = (blob or "").lower()
    if not name:
        return True
    if name in text:
        return True
    compact = _entity_slug(name)
    return bool(compact) and compact in _entity_slug(text)


def _normalize_understanding(data: dict[str, Any]) -> dict[str, Any]:
    period = data.get("period") if isinstance(data.get("period"), dict) else {}
    start = _parse_ymd(period.get("start"))
    end = _parse_ymd(period.get("end"))
    days = period.get("days")
    if start and end and not isinstance(days, int):
        days = (end - start).days + 1
    grain = str(data.get("grain") or "total").strip().lower()
    if grain in {"mom", "monthly", "month-on-month", "by_month", "by month"}:
        grain = "month"
    elif grain in {"wow", "weekly", "week-on-week", "by_week"}:
        grain = "week"
    explain = bool(data.get("explain_previous")) and grain == "total"
    return {
        "entity": str(data.get("entity") or "").strip(),
        "metric": str(data.get("metric") or "").strip(),
        "period": {"label": str(period.get("label") or "").strip(), "start": start, "end": end, "days": days},
        "grain": grain,
        "search": str(data.get("search") or "").strip(),
        "plan": str(data.get("plan") or "").strip(),
        "reason": str(data.get("reason") or "").strip(),
        "tool_hint": str(data.get("tool_hint") or "").strip(),
        "explain_previous": explain,
    }


async def understand_parts(query: str, history: list[dict] | None = None) -> list[dict[str, Any]]:
    today = now_local().date()
    prior = ""
    for item in reversed(history or []):
        if item.get("role") == "assistant" and str(item.get("content") or "").strip():
            prior = str(item.get("content") or "")[:500]
            break
    try:
        resp = await chat_complete(
            [
                {
                    "role": "user",
                    "content": (
                        "Think step by step, then split the user question into independent tool jobs. "
                        f"Today is {today.isoformat()} in Asia/Kolkata (IST). "
                        "Return json {\"parts\":[...]}. One part per distinct ask "
                        "(for example sales, search trends, and a forecast are three parts). "
                        "If the question is a single ask, return one part. Maximum 4 parts. "
                        "For each part: reason about entity, metric, and period; resolve relative time into start and end as YYYY-MM-DD; "
                        "set tool_hint to the tool type that reasoning picked "
                        "(headline scorecard, daily series, search trends, forecast), not a brand name. "
                        "grain is total, month, week, or day: month-on-month / by month is month, not a single range total. "
                        "A new breakdown is a new tool job, not explain_previous. "
                        "If the user omitted the entity or dates, copy them from the previous assistant answer. "
                        "If the entity is a code split by spaces, join it for the slug the tools use. "
                        "search is a short query for finding tools for that part only. "
                        "If the question is only about how a previous figure was produced, "
                        "set explain_previous true, leave search empty, and do not invent a new tool job.\n"
                        + (("Previous assistant answer:\n" + prior + "\n") if prior else "")
                        + f"Question: {query}\n"
                        "Each part json: "
                        '{"question":"","entity":"","metric":"","period":{"label":"","start":"","end":"","days":0},"grain":"total","search":"","plan":"","reason":"","tool_hint":"","explain_previous":false}'
                    ),
                }
            ],
            response_format={"type": "json_object"},
            max_tokens=500,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        raw_parts = data.get("parts") if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        raw_parts = None
    parts = []
    for item in raw_parts or []:
        if not isinstance(item, dict):
            continue
        part = _normalize_understanding(item)
        part["question"] = str(item.get("question") or "").strip()
        if part["question"] or part["metric"] or part["search"] or part["entity"]:
            parts.append(part)
    return parts or [_normalize_understanding({"search": query, "plan": query})]


async def understand_query(query: str, history: list[dict] | None = None) -> dict[str, Any]:
    parts = await understand_parts(query, history)
    return parts[0]


async def ensure_parts(query: str, bag: dict | None = None, history: list[dict] | None = None) -> list[dict[str, Any]]:
    if bag is not None and isinstance(bag.get("parts"), list) and bag["parts"]:
        return bag["parts"]
    parts = await understand_parts(query, history)
    if bag is not None:
        bag["parts"] = parts
        bag["understood"] = parts[0]
    return parts


async def ensure_understood(query: str, bag: dict | None = None) -> dict[str, Any]:
    parts = await ensure_parts(query, bag)
    return parts[0]


def period_window(understood: dict | None) -> dict[str, Any]:
    period = (understood or {}).get("period") if isinstance(understood, dict) else {}
    period = period if isinstance(period, dict) else {}
    return {
        "kind": period.get("label") or "unknown",
        "days": period.get("days"),
        "start": period.get("start"),
        "end": period.get("end"),
    }


def inherit_understood(part: dict[str, Any], prior_source: dict | None) -> dict[str, Any]:
    if not isinstance(part, dict) or not isinstance(prior_source, dict):
        return part
    prior = prior_source.get("understood") if isinstance(prior_source.get("understood"), dict) else {}
    result = prior_source.get("result") if isinstance(prior_source.get("result"), dict) else {}
    if not part.get("entity"):
        part["entity"] = str(prior.get("entity") or result.get("brand") or "")
    period = part.get("period") if isinstance(part.get("period"), dict) else {}
    prior_p = prior.get("period") if isinstance(prior.get("period"), dict) else {}
    if not period.get("start") and (prior_p.get("start") or prior_p.get("end")):
        merged = _normalize_understanding({"period": {**prior_p, "label": period.get("label") or prior_p.get("label")}})
        part["period"] = merged["period"]
    return part


def grain_windows(understood: dict | None) -> list[tuple[date, date, str]]:
    if not isinstance(understood, dict):
        return []
    grain = str(understood.get("grain") or "total")
    if grain != "month":
        return []
    asked = period_window(understood)
    start, end = asked.get("start"), asked.get("end")
    if not isinstance(start, date) or not isinstance(end, date) or start > end:
        return []
    out: list[tuple[date, date, str]] = []
    year, month = start.year, start.month
    while date(year, month, 1) <= end:
        last = date(year, month, monthrange(year, month)[1])
        first = max(start, date(year, month, 1))
        close = min(end, last)
        if first <= close:
            out.append((first, close, f"{year:04d}-{month:02d}"))
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
        if len(out) >= 12:
            break
    return out if len(out) > 1 else []


def has_month_breakdown(result: Any) -> bool:
    if isinstance(result, dict) and isinstance(result.get("by_month"), list) and len(result["by_month"]) >= 2:
        return True
    return False


def _metric_map(payload: Any) -> dict[str, float]:
    payload = unwrap_result(payload)
    nums: dict[str, float] = {}
    if not isinstance(payload, dict):
        return nums
    kpis = payload.get("kpis") if isinstance(payload.get("kpis"), dict) else {}
    totals = payload.get("totals") if isinstance(payload.get("totals"), dict) else {}
    for key in ("net_sales", "gross_sales", "net_orders", "net_units", "total_sales", "orders", "units"):
        val = kpis.get(key, totals.get(key, payload.get(key)))
        if isinstance(val, dict) and _is_num(val.get("value")):
            val = val["value"]
        if _is_num(val):
            nums[key] = float(val)
    return nums


def merge_window_results(rows: list[dict], understood: dict | None = None) -> dict[str, Any]:
    months: list[dict[str, Any]] = []
    totals: dict[str, float] = {}
    for row in rows:
        nums = _metric_map(row.get("result"))
        entry = {"month": row.get("month"), "start": row.get("start"), "end": row.get("end"), **nums}
        months.append(entry)
        for key, value in nums.items():
            totals[key] = totals.get(key, 0) + value
    asked = period_window(understood)
    return {
        "period": {
            "start": asked.get("start"),
            "end": asked.get("end"),
            "days": asked.get("days"),
        },
        "grain": "month",
        "by_month": months,
        "totals": totals,
    }


_ENTITY_FIELDS = ("brand", "brand_id", "entity")
_START_FIELDS = ("start", "since", "from", "date_from")
_END_FIELDS = ("end", "until", "to", "date_to")


def _named_in_contract(name: str, description: str, schema_fields: list[str] | None, error: str = "") -> bool:
    if schema_fields and name in schema_fields:
        return True
    blob = f"{description}\n{error}"
    return bool(re.search(rf"(?i)(?:\b|['\"`]){re.escape(name)}(?:\b|['\"`])", blob))


def contract_fields(description: str = "", schema_fields: list[str] | None = None, error: str = "") -> dict[str, list[str]]:
    schema = [str(item) for item in (schema_fields or [])]
    entity = [name for name in _ENTITY_FIELDS if _named_in_contract(name, description, schema, error)]
    start = [name for name in _START_FIELDS if _named_in_contract(name, description, schema, error)]
    end = [name for name in _END_FIELDS if _named_in_contract(name, description, schema, error)]
    windowed = bool(re.search(r"(?i)\b(date window|date range|period|preset|start|end|since|until)\b", description or error))
    if windowed and not start:
        start = ["start"]
    if windowed and not end:
        end = ["end"]
    return {"entity": entity, "start": start, "end": end}


def _iso(value: Any) -> str | None:
    if isinstance(value, date):
        return value.isoformat()
    parsed = _parse_ymd(value)
    return parsed.isoformat() if parsed else None


def bind_understood_args(
    args: dict,
    understood: dict | None = None,
    schema_fields: list[str] | None = None,
    description: str = "",
    error: str = "",
) -> dict:
    """Map understood entity/period onto names the tool description or schema actually uses."""
    out = dict(args or {})
    inner = dict(out.get("arguments") or {}) if isinstance(out.get("arguments"), dict) else {}
    understood = understood or {}
    period = understood.get("period") if isinstance(understood.get("period"), dict) else {}
    fields = contract_fields(description, schema_fields, error)
    entity = str(understood.get("entity") or "").strip()
    if entity and not any(inner.get(name) for name in _ENTITY_FIELDS):
        for name in fields["entity"]:
            inner[name] = _entity_slug(entity) if name in {"brand", "brand_id"} else entity
            break
    for name in ("brand", "brand_id"):
        value = inner.get(name)
        if isinstance(value, str) and value.strip():
            inner[name] = _entity_slug(value) or value
    start, end = _iso(period.get("start")), _iso(period.get("end"))
    if start and end:
        for name in fields["start"] or ["start"]:
            inner[name] = start
        for name in fields["end"] or ["end"]:
            inner[name] = end
    if inner:
        out["arguments"] = inner
    return out


def result_span(result: Any) -> dict[str, Any]:
    best: tuple[date | None, date | None, int] = (None, None, 0)
    for first, last, days in _result_windows(unwrap_result(result)):
        count = days or ((last - first).days + 1 if first and last else 0)
        if count >= best[2]:
            best = (first, last, count)
    return {"start": best[0], "end": best[1], "days": best[2] or None}


async def fill_execute_args(
    query: str,
    args: dict,
    understood: dict | None = None,
    schema_fields: list[str] | None = None,
    description: str = "",
    prior: dict | None = None,
) -> dict:
    out = dict(args or {})
    inner = dict(out.get("arguments") or {}) if isinstance(out.get("arguments"), dict) else {}
    try:
        today = now_local().date().isoformat()
        prior_note = ""
        if prior:
            prior_note = (
                f"Previous arguments json: {json.dumps(prior.get('arguments'), default=str)}\n"
                f"Tool returned period: {json.dumps(prior.get('returned'), default=str)}\n"
                "That period does not cover the asked period. "
                "Fill again using only argument names from this tool's description or schema.\n"
            )
        resp = await chat_complete(
            [
                {
                    "role": "user",
                    "content": (
                        "Fill this tool call from the user question. Return json "
                        '{"tool_name":"...","arguments":{}}. '
                        f"Today is {today} in Asia/Kolkata (IST). "
                        "Use only argument names from the tool description or known argument names. "
                        "Map the understood entity onto the entity/brand field the description uses. "
                        "Map the understood period onto the date-window fields the description uses. "
                        "If the description talks about a date window but does not name the fields, use start and end. "
                        "If a brand/entity field is a slug, join spaces in the entity. "
                        "Do not invent values that are not in the question or understood json.\n"
                        f"Question: {query}\n"
                        f"Understood json: {json.dumps(understood or {}, default=str)}\n"
                        f"Tool: {out.get('tool_name')}\n"
                        f"Known argument names: {json.dumps(schema_fields or [])}\n"
                        f"Description: {description}\n"
                        f"Current arguments json: {json.dumps(inner, default=str)}\n"
                        f"{prior_note}"
                    ),
                }
            ],
            response_format={"type": "json_object"},
            max_tokens=250,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception:  # noqa: BLE001
        data = {}
    if not isinstance(data, dict):
        data = {}
    if data.get("tool_name"):
        out["tool_name"] = data["tool_name"]
    extra = data.get("arguments") if isinstance(data.get("arguments"), dict) else {k: v for k, v in data.items() if k != "tool_name"}
    inner.update(extra)
    if inner:
        out["arguments"] = inner
    return bind_understood_args(out, understood, schema_fields, description)


def data_question(understood: dict | None) -> bool:
    if not isinstance(understood, dict):
        return False
    if understood.get("explain_previous"):
        return False
    period = understood.get("period") if isinstance(understood.get("period"), dict) else {}
    return bool(understood.get("entity") or understood.get("metric") or period.get("start") or period.get("end"))


def prefer_planned(candidates: list[dict] | None, tool_hint: str = "", understood: dict | None = None) -> list[dict]:
    listed = [item for item in (candidates or []) if item.get("public_name")]
    parts = [tool_hint or ""]
    if isinstance(understood, dict):
        parts.append(str(understood.get("metric") or ""))
        period = understood.get("period") if isinstance(understood.get("period"), dict) else {}
        parts.append(str(period.get("label") or ""))
        days = period.get("days")
        grain = str(understood.get("grain") or "")
        if grain == "month":
            parts.append("daily series month range")
        elif isinstance(days, int) and days > 1:
            parts.append("range daily series period")
        elif days == 1:
            parts.append("daily")
    hint = " ".join(parts).strip().lower()
    if not hint or len(listed) <= 1:
        return listed

    def rank(item: dict) -> tuple[int, int]:
        name = str(item.get("public_name") or "").lower()
        desc = str(item.get("description") or "").lower()
        if name == hint:
            return (0, 0)
        if hint in name:
            return (1, -len(hint))
        tokens = [tok for tok in hint.replace("-", " ").replace("_", " ").split() if len(tok) > 2]
        hits = sum(1 for tok in tokens if tok in name or tok in desc)
        if hits:
            return (2, -hits)
        return (5, 0)

    return sorted(listed, key=rank)


def next_unused(candidates: list[dict] | None, tried: list[str] | None) -> dict | None:
    blocked = {name for name in (tried or []) if name}
    for item in candidates or []:
        name = item.get("public_name")
        if name and name not in blocked:
            return {"tool_name": name, "arguments": {}}
    return None


def finalize_execute_result(
    best: Any,
    score: int,
    tried: list[str] | None,
    understood: dict | None,
    candidates: list[dict] | None = None,
) -> tuple[Any, dict[str, Any]]:
    used = [name for name in (tried or []) if name]
    understood_out = json.loads(json.dumps({k: (understood or {}).get(k) for k in ("entity", "metric", "period")}, default=str))
    evidence = {
        "complete": score >= 3,
        "score": score,
        "tried": used,
        "understood": understood_out,
    }
    remaining = [
        item.get("public_name")
        for item in (candidates or [])
        if item.get("public_name") and item.get("public_name") not in set(used)
    ]
    if score >= 1 and best is not None:
        evidence["complete"] = score >= 3
        evidence["usable"] = True
        tagged = _with_source_tool(best, used)
        if isinstance(tagged, dict) and score < 3:
            out = dict(tagged)
            out["coverage"] = "ok" if score >= 2 else "partial"
            out["asked"] = understood_out.get("period")
            return out, evidence
        return tagged, evidence
    return {
        "incomplete": True,
        "reason": "tool result did not cover the asked entity, metric, and period",
        "tried": used,
        "remaining": remaining,
        "understood": evidence["understood"],
        "next": "Execute a remaining tool, or the same tool with corrected arguments. Do not answer from this result.",
    }, evidence


def _with_source_tool(best: Any, used: list[str]) -> Any:
    if not isinstance(best, dict):
        return best
    out = dict(best)
    if used and not out.get("source_tool"):
        out["source_tool"] = used[-1]
    return out


def build_source(payload: Any, evidence: dict | None, used_tools: list[str] | None = None) -> dict[str, Any] | None:
    ev = evidence if isinstance(evidence, dict) else {}
    tried = [name for name in (ev.get("tried") or []) if name]
    extra = [name for name in (used_tools or []) if name and name not in tried]
    tools = extra + tried
    if not tools and payload is None:
        return None
    result = payload if isinstance(payload, dict) else {"result": payload}
    if isinstance(result, dict) and result.get("incomplete") and not result.get("source_tool"):
        return None
    text = json.dumps(result, default=str)
    source_tool = (result.get("source_tool") if isinstance(result, dict) else None) or (tried[-1] if tried else None)
    return {
        "source_tool": source_tool,
        "tools": tools,
        "understood": ev.get("understood"),
        "result": result if len(text) <= 2500 else {"preview": text[:2500], "source_tool": source_tool},
    }


async def plan_turn(
    query: str,
    understood: dict | None = None,
    candidates: list[dict] | None = None,
    rejected: dict | None = None,
) -> dict[str, Any]:
    today = now_local().date().isoformat()
    try:
        resp = await chat_complete(
            [
                {
                    "role": "user",
                    "content": (
                        "Plan how to answer this question, then reason about which tool to execute. "
                        "Return json "
                        '{"plan":"","reason":"","tool_name":"","arguments":{}}. '
                        f"Today is {today} in Asia/Kolkata (IST). "
                        "Use the understood entity, metric, and period. "
                        "Pick a tool from the candidate list when one is given. "
                        "Fill arguments using that tool's own names. "
                        "Do not invent figures.\n"
                        f"Question: {query}\n"
                        f"Understood json: {json.dumps(understood or {}, default=str)}\n"
                        f"Candidates json: {json.dumps(candidates or [], default=str)}\n"
                        f"Last rejected json: {json.dumps(rejected or {}, default=str)}"
                    ),
                }
            ],
            response_format={"type": "json_object"},
            max_tokens=300,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        if not isinstance(data, dict):
            data = {}
    except Exception:  # noqa: BLE001
        data = {}
    args = data.get("arguments") if isinstance(data.get("arguments"), dict) else {}
    return {
        "plan": str(data.get("plan") or "").strip(),
        "reason": str(data.get("reason") or "").strip(),
        "tool_name": str(data.get("tool_name") or "").strip(),
        "arguments": args,
    }


def parse_choice(data: dict | None, candidates: list[dict] | None) -> dict | None:
    listed = [item for item in (candidates or []) if item.get("public_name")]
    names = {item["public_name"] for item in listed}
    if not isinstance(data, dict):
        return None
    name = str(data.get("tool_name") or "").strip()
    if name not in names:
        return None
    args = data.get("arguments") if isinstance(data.get("arguments"), dict) else {}
    return {"tool_name": name, "arguments": args, "think": str(data.get("think") or data.get("reason") or "").strip()}


async def choose_and_fill(query: str, understood: dict | None, candidates: list[dict] | None) -> dict | None:
    listed = [
        {
            "public_name": item.get("public_name"),
            "description": (item.get("description") or "")[:500],
            "arguments": item.get("arguments") or [],
        }
        for item in (candidates or [])
        if item.get("public_name")
    ]
    if not listed:
        return None
    data: dict[str, Any] = {}
    if len(listed) > 1:
        try:
            today = now_local().date().isoformat()
            resp = await chat_complete(
                [
                    {
                        "role": "user",
                        "content": (
                            "Think step by step about which ONE tool to call, then return json "
                            '{"think":"","tool_name":"","arguments":{}}. '
                            f"Today is {today} in Asia/Kolkata (IST). "
                            "Reason about the asked entity, metric, period, and grain. "
                            "Pick the tool whose description matches that grain: "
                            "a single range total can use a window scorecard; "
                            "month-on-month needs a daily series, or the same window tool called once per month; "
                            "do not pick a whole-range dashboard if the user asked for months. "
                            "A day-by-day series needs a daily tool; trends and forecasts are their own tools. "
                            "Use only a tool_name from the list. "
                            "Fill arguments using only names from that tool's description or known argument names. "
                            "If the description talks about a date window but does not name the fields, use start and end. "
                            "Map the understood entity onto the brand/entity field the description uses. "
                            "If that field is a slug, join spaces in the entity. "
                            "Do not invent values.\n"
                            f"Question: {query}\n"
                            f"Understood json: {json.dumps(understood or {}, default=str)}\n"
                            f"Tools json: {json.dumps(listed)}"
                        ),
                    }
                ],
                response_format={"type": "json_object"},
                max_tokens=350,
            )
            data = json.loads(resp.choices[0].message.content or "{}")
        except Exception:  # noqa: BLE001
            data = {}
    pick = parse_choice(data, listed) or {"tool_name": listed[0]["public_name"], "arguments": {}}
    chosen = next(item for item in listed if item["public_name"] == pick["tool_name"])
    return bind_understood_args(
        pick,
        understood,
        chosen.get("arguments") if isinstance(chosen.get("arguments"), list) else [],
        chosen.get("description") or "",
    )


async def order_candidates(query: str, understood: dict | None, candidates: list[dict]) -> list[dict]:
    listed = [c for c in candidates if c.get("public_name")]
    if len(listed) <= 1:
        return listed
    try:
        resp = await chat_complete(
            [
                {
                    "role": "user",
                    "content": (
                        "Rank these tools for the user question. Return json "
                        '{"names":["tool_a","tool_b"]}. '
                        "Best match first. Use entity, metric, and period from the understood json. "
                        "Only use names from the list.\n"
                        f"Question: {query}\n"
                        f"Understood json: {json.dumps(understood or {}, default=str)}\n"
                        f"Tools json: {json.dumps(listed)}"
                    ),
                }
            ],
            response_format={"type": "json_object"},
            max_tokens=200,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        names = data.get("names") if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        names = None
    if not isinstance(names, list):
        return listed
    by_name = {c["public_name"]: c for c in listed}
    ordered = [by_name[name] for name in names if name in by_name]
    for item in listed:
        if item not in ordered:
            ordered.append(item)
    return ordered


def _tool_name(item: dict) -> str | None:
    for key in ("public_name", "name", "tool_name", "qualified_name"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _schema_fields(item: dict) -> tuple[list[str], list[str]]:
    schema = item.get("inputSchema") or item.get("input_schema") or item.get("parameters") or item.get("schema") or {}
    if not isinstance(schema, dict):
        return [], []
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    return list(props)[:24], [str(item) for item in required[:24]]


def slim_resolve(result: Any) -> dict:
    if not isinstance(result, dict):
        return {"note": str(result)[:200]}
    nodes = [result]
    for key in ("tool", "resolved", "match", "result", "data", "structuredContent", "structured_content"):
        value = result.get(key)
        if isinstance(value, dict):
            nodes.append(value)
        elif isinstance(value, list):
            nodes.extend(item for item in value[:3] if isinstance(item, dict))
    name = None
    description = ""
    arguments: list[str] = []
    required: list[str] = []
    for node in nodes:
        name = name or _tool_name(node)
        if not description and isinstance(node.get("description"), str):
            description = node["description"]
        fields, need = _schema_fields(node)
        if fields and not arguments:
            arguments, required = fields, need
    return {
        "public_name": name,
        "description": description[:240],
        "arguments": arguments,
        "required": required,
    }


def shrink_result(result: Any, limit: int = 8) -> Any:
    return _shrink(result, now_local().date().isoformat(), limit)


def _shrink(value: Any, today: str, limit: int) -> Any:
    if isinstance(value, dict):
        return {k: _shrink(v, today, limit) for k, v in value.items()}
    if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
        if all(item.get("date") for item in value):
            current = None
            previous = None
            for i, item in enumerate(value):
                if item.get("date") == today:
                    current = item
                    previous = value[i - 1] if i else None
                    break
            if current is None:
                return value[-2:] if len(value) > 2 else value
            return [item for item in (previous, current) if item]
        return value[:limit]
    return value


_HEAVY = {"detail", "details", "raw", "html", "debug", "payload", "trace", "embedding"}
_TARGET = 5500


def _chars(value: Any) -> int:
    try:
        return len(json.dumps(value, default=str))
    except Exception:
        return 0


def _strip_heavy(value: Any, depth: int = 0) -> Any:
    if depth > 14:
        return None
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if str(key).lower() in _HEAVY and _chars(item) > 400:
                continue
            out[key] = _strip_heavy(item, depth + 1)
        return out
    if isinstance(value, list):
        return [_strip_heavy(item, depth + 1) for item in value]
    return value


_DATE_KEYS = ("date", "day", "sale_date", "report_date", "dt", "business_date")


def _row_date(item: dict) -> str | None:
    for key in _DATE_KEYS:
        value = item.get(key)
        if isinstance(value, str) and len(value) >= 8:
            return value[:10]
    return None


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _summarize_dated(rows: list[dict], keep: set[str] | None = None) -> dict:
    if keep:
        rows = [row for row in rows if _row_date(row) in keep]
    dates = [item for item in (_row_date(row) for row in rows) if item]
    totals: dict[str, float] = {}
    for row in rows:
        for key, value in row.items():
            if _is_num(value):
                totals[key] = totals.get(key, 0) + value
    months: dict[str, dict[str, Any]] = {}
    for row in rows:
        day = _row_date(row)
        if not day:
            continue
        bucket = months.setdefault(day[:7], {"month": day[:7], "days": 0})
        bucket["days"] += 1
        for key, value in row.items():
            if _is_num(value):
                bucket[key] = bucket.get(key, 0) + value
    out = {
        "period": {
            "start": min(dates) if dates else None,
            "end": max(dates) if dates else None,
            "days": len(set(dates)),
        },
        "row_count": len(rows),
        "totals": totals,
    }
    if len(months) >= 2:
        out["by_month"] = [months[key] for key in sorted(months)]
    return out


def _preaggregate(value: Any, keep: set[str] | None = None, depth: int = 0) -> Any:
    if depth > 12:
        return None
    if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
        dated = sum(1 for item in value if _row_date(item))
        if dated >= max(3, len(value) // 2):
            return _summarize_dated(value, keep)
        return [_preaggregate(item, keep, depth + 1) for item in value[:20]]
    if isinstance(value, dict):
        return {key: _preaggregate(item, keep, depth + 1) for key, item in value.items()}
    return value


def _keep_dates(asked: dict[str, Any]) -> set[str] | None:
    start, end = asked.get("start"), asked.get("end")
    if not isinstance(start, date) or not isinstance(end, date):
        return None
    out: set[str] = set()
    cursor = start
    while cursor <= end and len(out) < 400:
        out.add(cursor.isoformat())
        cursor += timedelta(days=1)
    return out or None


def _parse_ymd(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _walk_periods(value: Any, found: list) -> None:
    if isinstance(value, dict):
        start = value.get("start") or value.get("from") or value.get("date_from")
        end = value.get("end") or value.get("to") or value.get("date_to")
        days = value.get("days")
        if start or end or isinstance(days, int):
            found.append((start, end, days))
        for item in value.values():
            _walk_periods(item, found)
    elif isinstance(value, list):
        for item in value[:8]:
            _walk_periods(item, found)


_TOTAL_KEYS = {"totals", "total_sales", "net_sales", "gross_sales", "sales", "sales_inr", "net_orders", "orders"}


def unwrap_result(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    text = result.get("text")
    if isinstance(text, str) and text.strip()[:1] in "{[":
        try:
            parsed = json.loads(text)
            if isinstance(parsed, (dict, list)):
                result = parsed
        except json.JSONDecodeError:
            pass
    if isinstance(result, dict):
        for key in ("result", "data", "structuredContent", "structured_content"):
            inner = result.get(key)
            if isinstance(inner, dict) and (_has_totals(inner) or inner.get("period") or _row_date(inner)):
                return inner
    return result


def _has_totals(result: Any, depth: int = 0) -> bool:
    if depth > 6:
        return False
    if isinstance(result, dict):
        keys = {str(key).lower() for key in result}
        if keys & _TOTAL_KEYS:
            return True
        if isinstance(result.get("totals"), dict) and result["totals"]:
            return True
        return any(_has_totals(item, depth + 1) for item in list(result.values())[:8])
    if isinstance(result, list):
        return any(_has_totals(item, depth + 1) for item in result[:8])
    return False


def _asked_days(asked: dict[str, Any]) -> int | None:
    days = asked.get("days")
    start, end = asked.get("start"), asked.get("end")
    if isinstance(days, int) and days > 0:
        return days
    if isinstance(start, date) and isinstance(end, date):
        return (end - start).days + 1
    return None


def _result_windows(result: Any) -> list[tuple[date | None, date | None, int | None]]:
    found: list[tuple[date | None, date | None, int | None]] = []
    if isinstance(result, dict):
        period = result.get("period")
        if isinstance(period, str):
            parsed = _parse_ymd(period)
            if parsed:
                found.append((parsed, parsed, 1))
        for key in _DATE_KEYS:
            parsed = _parse_ymd(result.get(key))
            if parsed:
                found.append((parsed, parsed, 1))
        walked: list = []
        _walk_periods(result, walked)
        for start, end, days in walked:
            first, last = _parse_ymd(start), _parse_ymd(end)
            count = days if isinstance(days, int) else None
            if first and last and count is None:
                count = (last - first).days + 1
            found.append((first, last, count))
    return found


def covers_asked_period(result: Any, understood: dict | None = None) -> bool:
    asked = period_window(understood)
    start_need, end_need = asked.get("start"), asked.get("end")
    if not isinstance(start_need, date) and not isinstance(end_need, date):
        return False
    need = _asked_days(asked)
    single = need == 1 or (isinstance(start_need, date) and start_need == end_need)
    target = end_need if isinstance(end_need, date) else start_need
    for first, last, days in _result_windows(result):
        if single:
            if target and last == target and first in {None, target} and days in {None, 1}:
                return True
            continue
        if first == start_need and last == end_need:
            return True
        if first and last and isinstance(start_need, date) and isinstance(end_need, date) and need:
            overlap_start = max(first, start_need)
            overlap_end = min(last, end_need)
            if overlap_end >= overlap_start:
                overlap = (overlap_end - overlap_start).days + 1
                if overlap >= max(1, int(need * 0.8)):
                    return True
        if need and isinstance(days, int) and days >= max(1, int(need * 0.8)):
            if last in {None, end_need} or first in {None, start_need}:
                return True
    return False


_SKIP_NUM = {"period", "start", "end", "days", "row_count", "date", "asked", "score"}


def _numbers(value: Any, found: list[float], depth: int = 0) -> None:
    if depth > 10:
        return
    if _is_num(value):
        found.append(float(value))
    elif isinstance(value, dict):
        if value.get("incomplete"):
            return
        for key, item in value.items():
            if str(key).lower() in _SKIP_NUM:
                continue
            _numbers(item, found, depth + 1)
    elif isinstance(value, list):
        for item in value[:30]:
            _numbers(item, found, depth + 1)


def _all_zero(result: Any) -> bool:
    if isinstance(result, dict) and isinstance(result.get("totals"), dict) and result["totals"]:
        nums = [float(v) for v in result["totals"].values() if _is_num(v)]
        return bool(nums) and all(n == 0 for n in nums)
    found: list[float] = []
    _numbers(result, found)
    return bool(found) and all(n == 0 for n in found)


def result_score(query: str, result: Any, tool_name: str | None = None, understood: dict | None = None) -> int:
    result = unwrap_result(result)
    if isinstance(result, dict) and result.get("incomplete"):
        return 0
    if _all_zero(result):
        return 0
    cover = covers_asked_period(result, understood)
    totals = _has_totals(result) or (
        isinstance(result, dict) and isinstance(result.get("totals"), dict) and bool(result.get("totals"))
    )
    entity_name = str((understood or {}).get("entity") or "").strip()
    blob = f"{json.dumps(result, default=str)} {tool_name or ''} {query}"
    entity = entity_mentioned(entity_name, blob)
    grain = str((understood or {}).get("grain") or "total")
    if grain == "month" and not has_month_breakdown(result):
        return 1 if totals and entity else 0
    if cover and totals and entity:
        return 3
    if cover and entity:
        return 2
    if totals and entity:
        return 1
    return 0


def _empty_period(value: Any) -> bool:
    if isinstance(value, dict):
        period = value.get("period") if isinstance(value.get("period"), dict) else {}
        if value.get("row_count") == 0 and not period.get("days"):
            return True
        return any(_empty_period(item) for item in value.values())
    if isinstance(value, list):
        return any(_empty_period(item) for item in value[:8])
    return False


async def compact_result(query: str, result: Any, understood: dict | None = None) -> Any:
    asked = period_window(understood)
    raw = _strip_heavy(unwrap_result(result))
    trimmed = _preaggregate(raw, _keep_dates(asked))
    if _empty_period(trimmed):
        trimmed = _preaggregate(raw, None)
    if _empty_period(trimmed):
        return {"incomplete": True, "reason": "no rows in the asked period", "asked": {k: str(v) if v is not None else None for k, v in asked.items()}}
    text = json.dumps(trimmed, default=str)
    if len(text) <= _TARGET:
        return trimmed
    try:
        resp = await chat_complete(
            [
                {
                    "role": "user",
                    "content": (
                        "A tool returned data for a user question. "
                        "Return compact JSON using only this data. "
                        "Keep the reporting window/period if present, plus the totals and breakdowns needed to answer. "
                        "If the data does not cover the asked period, include the period it did cover. "
                        "Do not invent figures.\n\n"
                        f"Question: {query}\n"
                        f"Understood json: {json.dumps(understood or {}, default=str)}\n\n"
                        f"Data:\n{text[:24000]}"
                    ),
                }
            ],
            response_format={"type": "json_object"},
            max_tokens=900,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        if not isinstance(data, dict) or not data:
            raise ValueError("empty compact")
        out = data
    except Exception as exc:  # noqa: BLE001
        out = trimmed if len(text) <= 12000 else {"note": "result too large", "preview": text[:4000], "error": str(exc)[:160]}
    return out


async def next_call_if_incomplete(
    query: str,
    pick: dict,
    result: Any,
    candidates: list[dict],
    tried: list[str] | None = None,
    understood: dict | None = None,
) -> dict | None:
    blocked = {name for name in (tried or []) if name} | {pick.get("tool_name")}
    listed = [
        {"public_name": c.get("public_name"), "description": (c.get("description") or "")[:180]}
        for c in candidates
        if c.get("public_name") and c.get("public_name") not in blocked
    ]
    if pick.get("tool_name"):
        listed = [{"public_name": pick.get("tool_name"), "description": "retry with corrected arguments"}] + listed
    if not listed:
        return None
    try:
        resp = await chat_complete(
            [
                {
                    "role": "user",
                    "content": (
                        "Decide if this tool result fully answers the user question. "
                        "Return json only. If it does, return {\"ok\":true}. "
                        "If the period, metric, or entity is wrong or incomplete, return "
                        "{\"tool_name\":\"...\",\"arguments\":{}} to try again. "
                        "Prefer a new tool if the current one is the wrong grain. "
                        "Retry the same tool only with corrected arguments. "
                        "A wider window does not answer a single day. A single day does not answer a range. "
                        "All-zero totals are not complete while other tools remain. "
                        "Fill arguments from the understood json using each tool's own parameter names.\n\n"
                        f"Today is {now_local().date().isoformat()} in Asia/Kolkata (IST).\n"
                        f"Question: {query}\n"
                        f"Understood json: {json.dumps(understood or {}, default=str)}\n"
                        f"Used: {json.dumps(pick, default=str)}\n"
                        f"Already tried: {json.dumps(sorted(blocked))}\n"
                        f"Other tools: {json.dumps(listed)}\nResult: {json.dumps(result, default=str)[:5000]}"
                    ),
                }
            ],
            response_format={"type": "json_object"},
            max_tokens=250,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    if data.get("ok") is True and result_score(query, result, pick.get("tool_name"), understood) >= 3:
        return None
    name = data.get("tool_name")
    args = data.get("arguments") if isinstance(data.get("arguments"), dict) else {}
    if not name:
        return None
    if name in blocked and not args:
        return None
    return {"tool_name": name, "arguments": args}


async def pick_downstream_call(query: str, candidates: list[dict]) -> dict | None:
    chosen = next((c for c in candidates if c.get("public_name")), None)
    if chosen is None:
        return None
    name = chosen["public_name"]
    resp = await chat_complete(
        [
            {
                "role": "user",
                "content": (
                    "Fill arguments for this tool from the user question. "
                    "Return JSON {\"arguments\":{}} only.\n\n"
                    f"Question: {query}\nTool: {name}\nDescription: {chosen.get('description') or ''}"
                ),
            }
        ],
        response_format={"type": "json_object"},
        max_tokens=200,
    )
    data = json.loads(resp.choices[0].message.content or "{}")
    args = data.get("arguments") if isinstance(data.get("arguments"), dict) else None
    if args is None and isinstance(data, dict):
        args = {k: v for k, v in data.items() if k != "tool_name"}
    return {"tool_name": name, "arguments": args or {}}


async def repair_args(query: str, tool: str, args: dict, error: str) -> dict | None:
    resp = await chat_complete(
        [
            {
                "role": "user",
                "content": (
                    "A tool call failed. Return JSON to retry. "
                    "Use the user question to fill anything the error says is missing. "
                    f"Tool: {tool}\nPrevious arguments: {json.dumps(args, default=str)}\n"
                    f"Error: {error}\nUser question: {query}\n"
                    "Return {\"tool_name\":\"...\",\"arguments\":{}} or {\"arguments\":{}} only."
                ),
            }
        ],
        response_format={"type": "json_object"},
        max_tokens=250,
    )
    data = json.loads(resp.choices[0].message.content or "{}")
    if not isinstance(data, dict) or not data:
        return None
    if tool == "gateway__execute":
        merged = dict(args)
        inner = dict(args.get("arguments") or {}) if isinstance(args.get("arguments"), dict) else {}
        extra = data.get("arguments") if isinstance(data.get("arguments"), dict) else {k: v for k, v in data.items() if k != "tool_name"}
        inner.update(extra)
        if data.get("tool_name"):
            merged["tool_name"] = data["tool_name"]
        merged["arguments"] = inner
        return merged
    merged = dict(args)
    extra = data.get("arguments") if isinstance(data.get("arguments"), dict) else data
    merged.update(extra)
    return merged
