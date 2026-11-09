"""Update feed for the desktop app, served from our API.

GitHub's release downloads often answer 504 for a few minutes, and an app that hits one simply doesn't update. This
fetches from GitHub with retries. Files are still verified by the app against the key it ships with, so this server
can't make it install anything GitHub's signed release doesn't contain.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

import httpx

from app.cache import get_redis

log = logging.getLogger(__name__)

REPO = "https://github.com/specialshweta26/Checkme/releases"
FEED_TTL_S = 120
FEED_KEY = "desktop:update:feed"
FEED_KEEP_S = 30 * 24 * 3600
MAX_ASSET_BYTES = 64 * 1024 * 1024
RETRY_DELAYS = (0.5, 1.0, 2.0, 4.0)
_TAG = re.compile(r"^desktop-v\d+\.\d+\.\d+$")
_NAME = re.compile(r"^Lumio[A-Za-z0-9_.\-]*\.(?:tar\.gz|exe|sig)$")
_ASSET_URL = re.compile(r"^https://github\.com/specialshweta26/Checkme/releases/download/(desktop-v\d+\.\d+\.\d+)/([^/?#]+)$")
_feed: tuple[float, dict] | None = None


class UpdateUnavailable(Exception):
    pass


async def _get(url: str, timeout: float) -> bytes:
    last = ""
    for delay in (*RETRY_DELAYS, None):
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
                res = await client.get(url)
            if res.status_code == 200:
                return res.content
            last = str(res.status_code)
            if res.status_code < 500:
                break
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        if delay is not None:
            await asyncio.sleep(delay)
    log.warning("desktop update: GitHub did not answer: %s", last)
    raise UpdateUnavailable(last)


def rewrite(feed: dict, base: str) -> dict:
    """The same feed, with its download addresses on this server. Signatures are untouched."""
    platforms = {}
    for name, row in (feed.get("platforms") or {}).items():
        hit = _ASSET_URL.match(str((row or {}).get("url") or ""))
        platforms[name] = {**row, "url": f"{base}/api/desktop/update/asset/{hit.group(1)}/{hit.group(2)}"} if hit else row
    return {**feed, "platforms": platforms}


async def latest(base: str) -> dict:
    """The newest release's feed. A short GitHub outage serves the last good copy."""
    global _feed
    now = time.monotonic()
    if _feed is None or now - _feed[0] > FEED_TTL_S:
        try:
            raw = await _get(f"{REPO}/latest/download/latest.json", 15)
            _feed = (now, json.loads(raw))
            await _save_feed(raw)
        except (UpdateUnavailable, ValueError):
            if _feed is None:
                saved = await _load_feed()  # a restart loses the copy in memory
                if saved is None:
                    raise UpdateUnavailable("no feed") from None
                _feed = (now, saved)
    return rewrite(_feed[1], base)


async def _save_feed(raw: bytes) -> None:
    try:
        await get_redis().set(FEED_KEY, raw, ex=FEED_KEEP_S)
    except Exception as exc:  # noqa: BLE001 - the copy in memory still serves
        log.info("desktop update: feed not saved: %s", type(exc).__name__)


async def _load_feed() -> dict | None:
    try:
        raw = await get_redis().get(FEED_KEY)
        return json.loads(raw) if raw else None
    except Exception as exc:  # noqa: BLE001
        log.info("desktop update: no saved feed: %s", type(exc).__name__)
        return None


async def asset(tag: str, name: str) -> bytes:
    if not _TAG.match(tag) or not _NAME.match(name):
        raise LookupError("not a release file")
    data = await _get(f"{REPO}/download/{tag}/{name}", 120)
    if len(data) > MAX_ASSET_BYTES:
        raise LookupError("too large")
    return data
