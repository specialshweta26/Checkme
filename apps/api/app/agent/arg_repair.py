"""Arguments a source ignored, sent again under the source's own names (docs/LLD.md "Argument repair").

Live, 24 Nov 2026: asked for a brand's sales today, the agent called funnel-analytics__get_daily_sales with
from_date/to_date (the names another source in the same gateway uses). That action takes start/end. A gateway doesn't
reject a name it doesn't know: it answered for its default 30 days, Lumio said the dates were ignored, and the agent
gave up, because this gateway's catalog doesn't describe the action's parameters and the note had no names to offer.

The answer itself usually says which names the source uses: it echoes them back with the values it actually used
("window": {"start": "2026-10-26", "end": "2026-11-24"}, "brand": "acme"). So when an answer shows an argument was not
applied (a wider period than sent, or a filter echoed with another value), Lumio renames the argument to the name
the source uses: from the catalog's parameters when it has them, else from the echo, else from names the action's
description mentions. It then asks once more. When the second answer matches what was asked, it is the one used, and
the names are kept for that action (per user, ARG_NAMES_S) so later calls are sent correctly the first time. This
works for any argument, not only dates.

Only names change, never values, so what the call reads stays the same and so does whether it needs approval.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

START = ("start", "start_date", "startDate", "from", "from_date", "fromDate", "date_from", "since", "begin", "date_start")
END = ("end", "end_date", "endDate", "to", "to_date", "toDate", "date_to", "until", "date_end")
DAY = ("date", "day", "on", "as_of", "asOf", "for_date")
WINDOW_KEYS = ("window", "range", "period", "date_range", "dates", "filters", "filter", "params", "query", "applied")
ARG_KEYS = ("arguments", "args", "params", "parameters", "input", "tool_input", "tool_args", "payload")
ARG_NAMES_S = 30 * 86400
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
# Words that say nothing about which filter a name is: "brand_id" and "store_id" share "id" but aren't the same thing.
_GENERIC = {"id", "ids", "name", "names", "code", "slug", "key", "value", "type", "filter", "by", "the", "of"}


def family(key: str) -> str:
    """'start', 'end', 'day' or '' for any other argument."""
    k = str(key)
    if k in START:
        return "start"
    if k in END:
        return "end"
    if k in DAY:
        return "day"
    return ""


def _tokens(key: str) -> set[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key)).lower()
    return {t for t in re.split(r"[_\-\s.]+", spaced) if t and t not in _GENERIC}


def _raw_tokens(key: str) -> set[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key)).lower()
    return {t for t in re.split(r"[_\-\s.]+", spaced) if t}


_DEFAULTS = {"all", "any", "*", "none", "null", "default", "", "total", "overall"}


def defaulted(wrong: list[tuple[str, str]], result: Any) -> bool:
    """The answer plainly fell back to its default for an ignored filter ("brand": "all")."""
    echo = echoes(result)
    return any(str(echo.get(other, "")).strip().casefold() in _DEFAULTS for _, other in wrong)


def inner(tool_name: str, args: dict) -> tuple[str | None, dict, bool]:
    """(key the action's arguments sit under, those arguments, whether they were JSON text) for a gateway execute call;
    (None, args, False) for a direct call."""
    from app.routing.retrieve import is_execute_tool

    if not isinstance(args, dict) or not is_execute_tool(tool_name):
        return None, args if isinstance(args, dict) else {}, False
    for key in ARG_KEYS:
        raw = args.get(key)
        if isinstance(raw, dict):
            return key, raw, False
        if isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                parsed = json.loads(raw)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return key, parsed, True
    return None, args, False


def wrap(args: dict, key: str | None, new_inner: dict, as_text: bool) -> dict:
    if key is None:
        return new_inner
    return {**args, key: json.dumps(new_inner) if as_text else new_inner}


def echoes(result: Any) -> dict[str, str]:
    """Plain values the answer reports back about itself: its own top-level fields and those of a window/filters
    object, also one level into a section ({"daily": {"brand": "acme", "start": ...}})."""
    found: dict[str, str] = {}
    if not isinstance(result, dict):
        return found

    def take(obj: dict) -> None:
        for k, v in obj.items():
            if isinstance(v, (str, int, float)) and not isinstance(v, bool) and str(k) not in found:
                found[str(k)] = str(v)

    take(result)
    for key in WINDOW_KEYS:
        if isinstance(result.get(key), dict):
            take(result[key])
    for value in list(result.values())[:12]:
        if isinstance(value, dict):
            take(value)
            for key in WINDOW_KEYS:
                if isinstance(value.get(key), dict):
                    take(value[key])
    return found


def _mentioned(description: str) -> list[str]:
    """Argument-like names an action's description mentions ("Omit start/end", "from_date and to_date")."""
    return list(dict.fromkeys(w for w in _WORD.findall(description or "") if family(w) or "_" in w))


def _numeric(value: Any) -> bool:
    try:
        float(str(value).replace(",", ""))
        return True
    except ValueError:
        return False


def applied(mapping: dict[str, str], sent: dict, result: Any) -> bool:
    """Every renamed filter now comes back with the value sent (dates are judged by their window instead)."""
    echo = echoes(result)
    for old, new in mapping.items():
        if family(old) or "+" in new or new not in echo:
            continue
        if str(echo[new]).strip().casefold() != str(sent.get(old)).strip().casefold():
            return False
    return True


def ignored_filters(sent: dict, result: Any) -> list[tuple[str, str]]:
    """(name sent, name the answer uses) for a filter the answer echoes under another name with another value: the
    source didn't recognise the name and answered for its default ("brand_name": "Acme" sent, "brand": "all" back)."""
    echo = echoes(result)
    if not echo:
        return []
    out = []
    for key, value in sent.items():
        if family(key) or key in echo or not isinstance(value, (str, int, float)) or isinstance(value, bool):
            continue
        mine = _tokens(key)
        if not mine:
            continue
        for other, got in echo.items():
            # The answer's name must be part of the one sent ("brand" in "brand_name", "channel" in "sales_channel"),
            # never more ("query_id" is a different thing from "query").
            if other in sent or family(other) or not (_tokens(other) and _raw_tokens(other) <= _raw_tokens(key)):
                continue
            if _numeric(got) != _numeric(value):
                continue  # a count or total that shares a word with the filter ("category_count"), not the filter itself
            if str(got).strip().casefold() != str(value).strip().casefold():
                out.append((key, other))
            break
    return out


def renames(sent: dict, result: Any, fields: list[str] | None = None, description: str = "", dates_ignored: bool = False) -> dict[str, str]:
    """Old name -> the source's name, for the arguments the answer shows were not applied. Empty when there is nothing
    to rename to (the agent is told instead)."""
    names = list(fields or [])
    echo = echoes(result)
    pool = names or [k for k, v in echo.items() if family(k) and _DATE.match(v)] or _mentioned(description)
    out: dict[str, str] = {}
    if dates_ignored:
        by_family: dict[str, str] = {}
        for name in pool:
            if family(name) and family(name) not in by_family:
                by_family[family(name)] = name
        for key in list(sent):
            fam = family(key)
            if not fam or key in pool:
                continue
            if fam == "day" and "start" in by_family and "end" in by_family and "day" not in by_family:
                out[key] = by_family["start"] + "+" + by_family["end"]  # one day sent; the source asks for a period
            elif fam in by_family and by_family[fam] != key and by_family[fam] not in sent:
                out[key] = by_family[fam]
    for key, other in ignored_filters(sent, result):
        if names and other not in names:
            continue
        out.setdefault(key, other)
    if names:  # the catalog's parameters are known: any other unknown name close to one of them
        for key in sent:
            if key in names or key in out:
                continue
            fam = family(key)
            match = [n for n in names if fam and family(n) == fam] or [n for n in names if not fam and _tokens(key) & _tokens(n) and not family(n)]
            if len(match) == 1 and match[0] not in sent:
                out[key] = match[0]
    return out


def renamed(sent: dict, mapping: dict[str, str]) -> dict:
    out: dict = {}
    for key, value in sent.items():
        target = mapping.get(key)
        if not target:
            out.setdefault(key, value)
        elif "+" in target:
            for part in target.split("+"):
                out[part] = value
        else:
            out[target] = value
    return out


def describe(mapping: dict[str, str]) -> str:
    return ", ".join(f"{old} as {new.replace('+', ' and ')}" for old, new in mapping.items())


def _key(user_id, slug: str, action: str) -> str:
    return f"arg-names:{user_id}:{slug}:{action}"


async def learned(user_id, slug: str, action: str) -> dict[str, str]:
    try:
        from app.cache import get_redis

        raw = await get_redis().get(_key(user_id, slug, action))
        got = json.loads(raw) if raw else {}
        return {str(k): str(v) for k, v in got.items()} if isinstance(got, dict) else {}
    except Exception:  # noqa: BLE001 - a cache miss just sends the call as the agent wrote it
        return {}


async def remember(user_id, slug: str, action: str, mapping: dict[str, str]) -> None:
    if not mapping:
        return
    try:
        from app.cache import get_redis

        merged = {**await learned(user_id, slug, action), **mapping}
        await get_redis().set(_key(user_id, slug, action), json.dumps(merged), ex=ARG_NAMES_S)
    except Exception:  # noqa: BLE001
        return


async def apply_learned(user_id, slug: str, tool_name: str, args: dict, action: str) -> tuple[dict, dict[str, str]]:
    """The call with names this source answered to before; (args, {}) when none apply."""
    key, sent, as_text = inner(tool_name, args)
    mapping = await learned(user_id, slug, action)
    use = {k: v for k, v in mapping.items() if k in sent and not any(p in sent for p in v.split("+"))}
    if not use:
        return args, {}
    return wrap(args, key, renamed(sent, use), as_text), use


async def forget(user_id, slug: str, action: str) -> None:
    try:
        from app.cache import get_redis

        await get_redis().delete(_key(user_id, slug, action))
    except Exception:  # noqa: BLE001
        return
