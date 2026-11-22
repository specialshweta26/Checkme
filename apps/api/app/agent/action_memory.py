"""Which gateway action answered which question, per user (docs/LLD.md "Action memory").

A data question on a gateway connector took 11-30 model steps: search the catalog (4-7 s a search, 3-6 searches),
read the schema, try one action, then another. Asking the same kind of question again started the hunt from zero.

After a turn, every gateway action that returned data is remembered under the user with the question it answered,
its arguments and the parameters the catalog gave it. At the start of a turn, the remembered actions whose questions
share words with this one are offered in the turn's context, and their parameters are loaded as if this turn's search
had found them, so the agent can call one straight away. It's a hint: the agent still searches when none fits, and
a failing action is searched for as before. Kept ACTION_MEMORY_DAYS days; 0 turns it off. Never raises.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

log = logging.getLogger(__name__)

MAX_ACTIONS = 60  # per user
MAX_QUESTIONS = 4  # per action
SHOWN = 4
_SEP = "\x1f"
_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an and are as at be by can do does for from give how i in is it last me my of on or please show tell that the "
    "this to was were what which who with you your yesterday today week weeks month months day days".split()
)


def _days() -> int:
    from app.config import get_settings

    return int(getattr(get_settings(), "action_memory_days", 14) or 0)


def _key(user_id) -> str:
    return f"actmem:{user_id}"


def words(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 1}


def _has_data(data: Any) -> bool:
    if data is None or data == {} or data == [] or data == "":
        return False
    if isinstance(data, dict) and (data.get("error") or data.get("isError") or data.get("is_error")):
        return False
    return True


def _inner_args(args: dict) -> dict:
    from app.agent.deep import _EXECUTE_ARG_KEYS

    for key in _EXECUTE_ARG_KEYS:
        raw = args.get(key)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                continue
        if isinstance(raw, dict):
            return raw
    return {}


def answered(ctx) -> list[tuple[str, str, dict]]:
    """(connector slug, action, arguments) for every gateway action this turn that returned data."""
    from app.agent import approval
    from app.routing.retrieve import is_catalog_tool, is_execute_tool

    out: list[tuple[str, str, dict]] = []
    for row in getattr(ctx, "results", None) or []:
        slug, tool, args = row.get("connector_slug"), str(row.get("tool_name") or ""), row.get("args") or {}
        if not slug or not is_execute_tool(tool) or not _has_data(row.get("data")):
            continue
        for action in approval.nested_actions(tool, args):
            if action != tool and not is_execute_tool(action) and not is_catalog_tool(action):
                out.append((slug, action, _inner_args(args)))
    return out


async def remember(ctx, query: str) -> None:
    days = _days()
    found = answered(ctx)
    user_id = getattr(getattr(ctx, "user", None), "id", None)
    if days <= 0 or not found or user_id is None or not words(query):
        return
    try:
        from app.cache import get_redis

        r = get_redis()
        key = _key(user_id)
        now = int(time.time())
        for slug, action, args in found:
            field = f"{slug}{_SEP}{action}"
            raw = await r.hget(key, field)
            entry = json.loads(raw) if raw else {"slug": slug, "action": action, "questions": [], "uses": 0}
            questions = [q for q in entry.get("questions") or [] if q != query]
            entry["questions"] = ([query[:300]] + questions)[:MAX_QUESTIONS]
            entry["uses"] = int(entry.get("uses") or 0) + 1
            entry["last"] = now
            entry["args"] = json.dumps(args, default=str)[:400] if args else ""
            known = (getattr(ctx, "gateway_args", None) or {}).get((slug, action))
            if isinstance(known, dict):
                params = {k: known.get(k) for k in ("fields", "required", "read_only") if known.get(k) is not None}
                schema = json.dumps(known.get("schema") or {}, default=str)
                if len(schema) <= 6000:
                    params["schema"] = known.get("schema") or {}
                entry["params"] = params
            await r.hset(key, field, json.dumps(entry, default=str))
        every = await r.hgetall(key)
        if len(every) > MAX_ACTIONS:  # drop the least recently used
            ranked = sorted(every.items(), key=lambda kv: json.loads(kv[1]).get("last", 0))
            await r.hdel(key, *[f for f, _ in ranked[: len(every) - MAX_ACTIONS]])
        await r.expire(key, days * 86400)
    except Exception:  # noqa: BLE001
        log.info("action memory not saved", exc_info=True)


def rank(entries: list[dict], query: str, slugs: set[str]) -> list[dict]:
    """The remembered actions that fit this question best: shared words with the questions they answered (and their
    own name), then how often and how recently they were used."""
    asked = words(query)
    if not asked:
        return []
    scored = []
    for e in entries:
        if e.get("slug") not in slugs:
            continue
        theirs = set().union(*(words(q) for q in e.get("questions") or [""])) | words(str(e.get("action", "")).replace("_", " "))
        shared = len(asked & theirs)
        if shared:
            scored.append((shared, int(e.get("uses") or 0), int(e.get("last") or 0), e))
    scored.sort(key=lambda t: t[:3], reverse=True)
    return [t[3] for t in scored[:SHOWN]]


async def recall(ctx, query: str) -> list[dict]:
    user_id = getattr(getattr(ctx, "user", None), "id", None)
    if _days() <= 0 or user_id is None:
        return []
    try:
        from app.cache import get_redis

        raw = await get_redis().hgetall(_key(user_id))
        entries = [json.loads(v) for v in raw.values()]
    except Exception:  # noqa: BLE001
        return []
    return rank(entries, query, set((getattr(ctx, "connectors", None) or {}).keys()))


def preload(ctx, rows: list[dict]) -> None:
    """The remembered actions' parameters, as if this turn's catalog search had found them."""
    for e in rows:
        slug, action, params = e.get("slug"), e.get("action"), e.get("params")
        if not slug or not action:
            continue
        if isinstance(params, dict) and params:
            ctx.gateway_args.setdefault((slug, action), {"fields": list(params.get("fields") or []),
                                                         "required": list(params.get("required") or []),
                                                         "read_only": bool(params.get("read_only")),
                                                         "schema": params.get("schema") or {}})  # fmt: skip
        ctx.gateway_args.setdefault(("__seen__", slug), set()).add(action)


def block(rows: list[dict]) -> str:
    if not rows:
        return ""
    lines = [
        "Actions that answered questions like this before (from this user's earlier chats). If one fits, call it "
        "straight away with call_tool on its connector, with arguments for this question (dates, names, metrics); "
        "search the catalog only when none fits or it fails. Several that are needed: call them in one step."
    ]
    for e in rows:
        params = e.get("params") or {}
        fields = ", ".join(str(f) for f in (params.get("fields") or [])[:12])
        asked = (e.get("questions") or [""])[0]
        line = f"- {e['slug']}: {e['action']} — answered “{asked[:140]}”"
        if fields:
            line += f"; parameters: {fields}"
        if e.get("args"):
            line += f"; last arguments: {e['args'][:240]}"
        lines.append(line)
    return "\n".join(lines)
