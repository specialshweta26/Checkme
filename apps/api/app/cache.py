from __future__ import annotations

import logging

import redis.asyncio as redis

from app.config import get_settings

log = logging.getLogger(__name__)

_client: redis.Redis | None = None

# Starter Key Value is 256MB. Forking a snapshot above ~180MB OOMs the instance.
_RECLAIM_BYTES = 180 * 1024 * 1024


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(
            get_settings().redis_url,
            decode_responses=False,
            health_check_interval=15,
            socket_connect_timeout=2,
            socket_timeout=2,
            retry_on_timeout=True,
        )
    return _client


def reset_redis_for_tests() -> None:
    global _client
    _client = None


async def rate_limited(key: str, limit: int, window_s: int = 60) -> bool:
    try:
        r = get_redis()
        k = f"rl:{key}"
        n = await r.incr(k)
        if n == 1:
            await r.expire(k, window_s)
        return n > limit
    except Exception:  # noqa: BLE001 — Redis down must not 500 the request
        return False


def _used_memory(info: dict) -> int:
    raw = info.get("used_memory")
    if raw is None:
        raw = info.get(b"used_memory")
    return int(raw or 0)


async def reclaim_if_full(client: redis.Redis | None = None) -> dict:
    """Drop rebuildable cache so a 256MB instance can start. Safe: sessions/tokens rehydrate from Postgres."""
    r = client or get_redis()
    try:
        used = _used_memory(await r.info("memory"))
    except Exception as exc:  # noqa: BLE001
        log.warning("redis reclaim skipped: %s", type(exc).__name__)
        return {"ok": False, "error": type(exc).__name__}

    flushed = False
    deleted = 0
    if used >= _RECLAIM_BYTES:
        await r.flushdb()
        flushed = True
    else:
        cursor: int | bytes = 0
        while True:
            cursor, keys = await r.scan(cursor, match="emb:*", count=400)
            if keys:
                await r.delete(*keys)
                deleted += len(keys)
            if cursor in (0, b"0", "0"):
                break
        await r.delete("composio:toolkits:v1")
        used = _used_memory(await r.info("memory"))
        if used >= _RECLAIM_BYTES:
            await r.flushdb()
            flushed = True
            deleted = 0
    used = _used_memory(await r.info("memory"))
    log.info("redis reclaim flushed=%s deleted_emb=%s used=%s", flushed, deleted, used)
    return {"ok": True, "flushed": flushed, "deleted_emb": deleted, "used_memory": used}
