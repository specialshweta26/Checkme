"""Actions that leave the app happen once, even if a request is retried.

Sending, replying, forwarding and creating events or meeting links get an idempotency key (user + connector + action +
exact content) claimed in Redis before the provider is called, for WINDOW_S:
- a retry after success returns the first result and sends nothing;
- a retry while the first is in flight, or after the provider timed out (it may have gone through), is refused with a
  message to check the Sent folder;
- if the provider clearly refused the action, the key is released so a retry can work.
Only the same content is blocked: a different message, or the same one after the window, goes through. If Redis is
unreachable the action goes ahead (blocking every send would be worse).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

WINDOW_S = 600
MAIL_SIDE_EFFECTS = {"send_message", "reply", "forward", "send_draft", "create_event", "create_teams_meeting", "create_google_meet"}
# Tool names on other connectors (MCP, Composio) that send or create something outside the app.
SIDE_EFFECT_NAME = re.compile(r"(^|[_.-])(send|reply|forward|post_message|create_event|create_meeting|schedule_meeting|"
                              r"place_order|checkout|create_invoice|transfer|pay)([_.-]|$)", re.I)
_UNCERTAIN = ("timeout", "timed out", "connection reset", "connection aborted", "remote end closed", "502", "503", "504")


def is_side_effect(tool: str, builtin_mail: bool) -> bool:
    return tool in MAIL_SIDE_EFFECTS if builtin_mail else bool(SIDE_EFFECT_NAME.search(tool or ""))


def key_for(user_id, connector: str, tool: str, args: dict) -> str:
    body = json.dumps(args or {}, sort_keys=True, default=str, ensure_ascii=False)
    digest = hashlib.sha256(f"{user_id}|{connector}|{tool}|{body}".encode()).hexdigest()
    return f"idem:{digest}"


def _uncertain(exc: BaseException) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True
    text = f"{type(exc).__name__} {exc}".lower()
    return any(t in text for t in _UNCERTAIN)


def _when(ts: float) -> str:
    return time.strftime("%H:%M UTC", time.gmtime(ts))


async def once(user_id, connector: str, tool: str, args: dict, run: Callable[[], Awaitable[Any]]) -> Any:
    from app.cache import get_redis

    key = key_for(user_id, connector, tool, args)
    now = time.time()
    try:
        redis = get_redis()
        claimed = await redis.set(key, json.dumps({"state": "pending", "at": now}), nx=True, ex=WINDOW_S)
    except Exception:  # noqa: BLE001 - no Redis: go ahead rather than block every send
        log.warning("idempotency unavailable; %s goes ahead", tool, exc_info=True)
        return await run()
    if not claimed:
        try:
            seen = json.loads(await redis.get(key) or "{}")
        except Exception:  # noqa: BLE001
            seen = {}
        state, at = seen.get("state"), float(seen.get("at") or now)
        log.info("idempotency: %s repeated within %ss (%s)", tool, WINDOW_S, state)
        if state == "done":
            result = seen.get("result")
            note = f"Already done at {_when(at)}; not repeated."
            return {**result, "duplicate": True, "note": note} if isinstance(result, dict) else {"duplicate": True, "note": note, "result": result}
        if state == "pending":
            return {"error": "This exact message is already being sent. Check your Sent folder before trying again."}
        return {"error": (f"A send with exactly this content was attempted at {_when(at)} and may have gone through (the mail "
                          "server didn't answer in time). Check your Sent folder; to send it again anyway, wait 10 minutes "
                          "or change the text slightly.")}
    try:
        result = await run()
    except BaseException as exc:
        try:
            if _uncertain(exc):
                await redis.set(key, json.dumps({"state": "unknown", "at": now}), ex=WINDOW_S)
            else:
                await redis.delete(key)  # it clearly didn't happen: a retry may try again
        except Exception:  # noqa: BLE001
            pass
        raise
    if isinstance(result, dict) and result.get("error"):
        try:
            await redis.delete(key)  # the provider refused: nothing was sent
        except Exception:  # noqa: BLE001
            pass
        return result
    try:
        small = result if isinstance(result, (dict, list, str, int, float, bool, type(None))) else str(result)
        payload = json.dumps({"state": "done", "at": now, "result": small}, default=str)
        if len(payload) > 8000:
            payload = json.dumps({"state": "done", "at": now, "result": {"ok": True}})
        await redis.set(key, payload, ex=WINDOW_S)
    except Exception:  # noqa: BLE001
        log.warning("idempotency result not stored", exc_info=True)
    return result
