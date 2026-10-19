from __future__ import annotations

import redis.asyncio as redis

from app.config import get_settings

_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(get_settings().redis_url, decode_responses=False)
    return _client


async def rate_limited(key: str, limit: int, window_s: int = 60) -> bool:
    r = get_redis()
    k = f"rl:{key}"
    n = await r.incr(k)
    if n == 1:
        await r.expire(k, window_s)
    return n > limit
