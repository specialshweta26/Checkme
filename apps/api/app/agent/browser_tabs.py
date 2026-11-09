"""Gives each chat its own browser tab, so agents working at the same time don't drive each other's pages.

A chat keeps its tab between turns, so "carry on" still finds its page. A tab nobody has touched for a while is reused
before opening another. A browser that can't report tabs (an older desktop app) simply stays on one page, as before.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.cache import get_redis

log = logging.getLogger(__name__)

PAGE_TOOLS = {"browser_navigate", "browser_snapshot", "browser_act", "browser_collect", "browser_download", "browser_page_log", "browse_open", "browse_act", "browse_screenshot"}
HOLD_S = 2 * 3600
IDLE_S = 600


def _key(user_id: Any, where: str) -> str:
    return f"browser:tabs:{user_id}:{where}"


def _chat(ctx: Any) -> str:
    return str(getattr(ctx, "session_id", "") or "")


def choose(mine: dict | None, others: dict[str, dict], now: float) -> tuple[str, str]:
    """(tab to use, how): "mine", "claim" the page when no chat holds one, "reuse" an idle tab's, or "new"."""
    if mine and mine.get("tab"):
        return str(mine["tab"]), "mine"
    if not others:
        return "", "claim"
    idle = [(row.get("at", 0), chat) for chat, row in others.items() if now - float(row.get("at", 0)) > IDLE_S]
    if idle:
        return str(others[min(idle)[1]].get("tab") or ""), "reuse"
    return "", "new"


async def tab_for(ctx: Any, tool: str, args: dict | None, where: str, call: Any) -> dict | None:
    """``args`` with this chat's tab added, or None to leave them alone. ``call`` runs a browser tool: call(tool, args)."""
    chat = _chat(ctx)
    user_id = getattr(getattr(ctx, "user", None), "id", None)
    if tool not in PAGE_TOOLS or (args or {}).get("tab"):
        return None
    if not chat or user_id is None:
        log.info("browser tabs: no chat for this call (chat=%s user=%s)", bool(chat), user_id is not None)
        return None
    try:
        redis = get_redis()
        key = _key(user_id, where)
        raw = await redis.hgetall(key)
        held = {}
        for name, value in (raw or {}).items():
            name = name.decode() if isinstance(name, (bytes, bytearray)) else str(name)
            held[name] = json.loads(value)
        mine = held.pop(chat, None)
        now = time.time()
        tab, how = choose(mine, held, now)
        if how == "reuse":
            old = next((c for c, row in held.items() if str(row.get("tab")) == tab), "")
            if old:
                await redis.hdel(key, old)
        elif how == "claim":
            listing = await call("browser_tabs", {})
            tabs = listing.get("tabs") if isinstance(listing, dict) else None
            if not tabs:
                log.info("browser tabs: this browser listed none (keys=%s)", sorted(listing)[:8] if isinstance(listing, dict) else type(listing).__name__)
                return None
            active = next((row for row in tabs if row.get("active")), None) or next(
                (row for row in tabs if str(row.get("url") or "").startswith("http")), tabs[0]
            )
            tab = str(active.get("tab") or "")
        elif how == "new":
            opened = await call("browser_new_tab", {"background": True})
            tab = str(opened.get("tab") or "") if isinstance(opened, dict) else ""
        if not tab:
            log.info("browser tabs: no tab to use (how=%s)", how)
            return None
        await redis.hset(key, chat, json.dumps({"tab": tab, "at": now}))
        await redis.expire(key, HOLD_S)
        return {**(args or {}), "tab": tab}
    except Exception as exc:  # noqa: BLE001 - a missing tab must never stop the task
        log.info("browser tabs: staying on the current page (%s)", type(exc).__name__)
        return None


async def forget(ctx: Any, where: str) -> None:
    """This chat's tab has gone (closed by hand, or the browser restarted)."""
    user_id = getattr(getattr(ctx, "user", None), "id", None)
    if user_id is None or not _chat(ctx):
        return
    try:
        await get_redis().hdel(_key(user_id, where), _chat(ctx))
    except Exception:  # noqa: BLE001
        pass


def tab_gone(result: Any) -> bool:
    return isinstance(result, dict) and result.get("ok") is False and str(result.get("error") or "").lower().startswith(("no tab ", "unknown tab"))
