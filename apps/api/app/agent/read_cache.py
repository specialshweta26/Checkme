"""Short-lived cache of read-only tool results, per user (docs/LLD.md "Read cache").

Asking the same thing twice in a few minutes ("today's sales", then "and by region?") re-ran the same connector reads.
A plain read (one its server marks read-only, with no approval involved) is kept for READ_CACHE_S seconds under the
user, the connector, the tool and its exact arguments. Any other call to that connector by the user (a send, an
update) starts a new generation, so a read after a write is never served from before it. Never raises: a Redis error
just runs the tool.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

log = logging.getLogger(__name__)

NEVER = {"computer", "browser", "desktop", "agent_browser"}  # live screens: always fresh
MAX_BYTES = 256_000


def _ttl() -> int:
    from app.config import get_settings

    return int(getattr(get_settings(), "read_cache_s", 120) or 0)


_DAY_KEYS = ("date", "day", "on", "as_of", "asOf")


def _dates(args: Any, depth: int = 0) -> list[str]:
    """The end (or single) dates a call names, at the top level or one argument object down (a gateway's execute)."""
    from app.agent.windows import END_KEYS

    if not isinstance(args, dict) or depth > 2:
        return []
    found = [str(args[k]).strip()[:10] for k in (*END_KEYS, *_DAY_KEYS) if isinstance(args.get(k), str)]
    for value in args.values():
        if isinstance(value, dict):
            found += _dates(value, depth + 1)
        elif isinstance(value, str) and value.strip().startswith("{"):
            try:
                found += _dates(json.loads(value), depth + 1)
            except ValueError:
                pass
    return found


def ttl_for(args: Any, tz_name: str | None = None) -> int:
    """How long to keep this read: longer when every date it names is before today (a closed period)."""
    from datetime import date, datetime
    from zoneinfo import ZoneInfo

    from app.config import get_settings

    base = _ttl()
    past = int(getattr(get_settings(), "read_cache_past_s", 0) or 0)
    if base <= 0 or past <= base:
        return base
    days = []
    for raw in _dates(args):
        try:
            days.append(date.fromisoformat(raw))
        except ValueError:
            return base  # a date Lumio can't read: don't assume it's closed
    if not days:
        return base
    try:
        today = datetime.now(ZoneInfo(tz_name or "UTC")).date()
    except Exception:  # noqa: BLE001
        today = datetime.utcnow().date()
    return past if all(d < today for d in days) else base


def usable(slug: str) -> bool:
    return _ttl() > 0 and slug not in NEVER and not slug.startswith("computer")


def _gen_key(user_id, slug: str) -> str:
    return f"rc-gen:{user_id}:{slug}"


async def _key(r, user_id, slug: str, tool: str, args: dict) -> str:
    gen = await r.get(_gen_key(user_id, slug))
    raw = json.dumps([slug, tool, args], sort_keys=True, default=str)
    return f"rc:{user_id}:{slug}:{(gen or b'0').decode() if isinstance(gen, bytes) else gen}:{hashlib.sha256(raw.encode()).hexdigest()[:32]}"


async def get(user_id, slug: str, tool: str, args: dict, ttl: int | None = None) -> Any:
    """The cached result, or None. ``ttl=0`` (a cache turned off by its own setting) never hits."""
    if not usable(slug) or ttl == 0:
        return None
    try:
        from app.cache import get_redis

        r = get_redis()
        hit = await r.get(await _key(r, user_id, slug, tool, args))
        return json.loads(hit) if hit else None
    except Exception:  # noqa: BLE001
        return None


async def put(user_id, slug: str, tool: str, args: dict, result: Any, ttl: int | None = None) -> None:
    if not usable(slug) or ttl == 0 or result is None or (isinstance(result, dict) and result.get("error")):
        return
    try:
        from app.agent.jsonsize import bounded_dumps

        body = bounded_dumps(result, MAX_BYTES)  # a big table is not encoded whole only to be dropped
        if body is None:
            return
        from app.cache import get_redis

        r = get_redis()
        await r.set(await _key(r, user_id, slug, tool, args), body, ex=ttl or _ttl())
    except Exception:  # noqa: BLE001
        return


async def wrote(user_id, slug: str) -> None:
    """A call that may change data: reads cached before it no longer count."""
    try:
        from app.cache import get_redis

        r = get_redis()
        k = _gen_key(user_id, slug)
        await r.incr(k)
        await r.expire(k, 86400)
    except Exception:  # noqa: BLE001
        return
