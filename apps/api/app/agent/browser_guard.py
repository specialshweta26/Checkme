"""Stops a browser turn from repeating actions that change nothing.

A single turn once ran the same dropdown action about 80 times over 15 minutes, because each one reported success while
the page stayed the same. This watches results, whichever browser ran them: an identical action that left the page
unchanged is refused after two tries, and a long run of actions that change nothing ends the browser work for the turn.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

ACT_TOOLS = {"browser_act", "browse_act"}
PASSIVE_KINDS = {"scroll", "wait", "wait_for", "hover"}  # reading a page legitimately changes nothing
SAME_ACTION_LIMIT = 2  # identical action, same page, no change: this many tries
NO_CHANGE_WARN = 5  # different actions in a row that changed nothing: warn
NO_CHANGE_STOP = 10  # ... stop


def _safe_error(result: dict) -> str:
    """The error's cause for the log, without quoted text: a "no option" message repeats what was typed."""
    return re.sub(r'"[^"]*"', '"…"', str(result.get("error") or ""))[:160]


def normalize(args: dict | None) -> dict | None:
    """Actions may give a dropdown's choice as value; the browsers read text."""
    rows = (args or {}).get("actions")
    if not isinstance(rows, list):
        return args
    fixed = []
    for row in rows:
        if isinstance(row, dict) and not row.get("text") and row.get("value") not in (None, ""):
            row = {**row, "text": str(row["value"])}
        fixed.append(row)
    return {**(args or {}), "actions": fixed}


def _state(ctx: Any) -> dict | None:
    state = getattr(ctx, "_browser_guard", None)
    if state is None:
        state = {"page": "", "tries": {}, "idle": 0, "stuck": False}
        try:
            ctx._browser_guard = state
        except AttributeError:
            return None  # a context that takes no attributes simply runs unguarded
    return state


def is_stuck(ctx: Any) -> bool:
    state = getattr(ctx, "_browser_guard", None)
    return bool(isinstance(state, dict) and state.get("stuck"))


def _actions(args: dict | None) -> list:
    rows = (args or {}).get("actions")
    if isinstance(rows, list):
        return rows
    return [args] if args else []


def _passive(args: dict | None) -> bool:
    rows = _actions(args)
    return bool(rows) and all(isinstance(r, dict) and str(r.get("kind") or "").lower() in PASSIVE_KINDS for r in rows)


def _action_key(tool: str, args: dict | None) -> str:
    # Only a digest is kept: actions can hold what was typed into a field.
    raw = json.dumps({"tool": tool, "args": args or {}}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _page_key(result: Any) -> str:
    if not isinstance(result, dict) or not (result.get("url") or result.get("tree") or result.get("text")):
        return ""
    raw = "\x00".join(str(result.get(k) or "") for k in ("url", "title", "text", "tree", "composer"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _stop_reply(state: dict) -> dict:
    state["stuck"] = True
    return {
        "ok": False,
        "stopped": True,
        "error": (
            "Stopped: the last actions on this page changed nothing, so more of them won't help. Do not call browser_act "
            "again this turn. Tell the user plainly which step failed, what you tried and what you did finish."
        ),
    }


def before(ctx: Any, tool: str, args: dict | None) -> dict | None:
    """A reply to return instead of running the action, or None to run it."""
    if tool not in ACT_TOOLS or _passive(args):
        return None
    state = _state(ctx)
    if state is None:
        return None
    if state["stuck"]:
        return _stop_reply(state)
    key = f"{_action_key(tool, args)}:{state['page']}"
    tries = state["tries"].get(key, 0)
    if tries >= SAME_ACTION_LIMIT:
        state["tries"][key] = tries + 1
        state["idle"] += 1
        log.info("browser.guard refused a repeat tries=%s idle=%s", tries + 1, state["idle"])
        if state["idle"] >= NO_CHANGE_STOP:
            return _stop_reply(state)
        return {
            "ok": False,
            "repeated": True,
            "error": (
                f"Not run: you already did exactly this {tries} times and the page did not change. Don't repeat it. Read the "
                "page again with browser_snapshot, use a different element or value, or look at the screen with "
                "computer_observe. If there is no other way, tell the user which step is stuck."
            ),
        }
    return None


def after(ctx: Any, tool: str, args: dict | None, result: Any) -> Any:
    state = _state(ctx)
    if state is None:
        return result
    page = _page_key(result)
    if tool not in ACT_TOOLS:
        if page:  # a navigation or a fresh read tells us which page we are on
            if page != state["page"]:
                state["idle"] = 0
            state["page"] = page
        return result
    if _passive(args):
        if page:
            state["page"] = page
        return result
    before_page = state["page"]
    changed = bool(page) and page != before_page
    failed = isinstance(result, dict) and result.get("ok") is False
    log.info(
        "browser.step kinds=%s ok=%s changed=%s idle=%s tab=%s err=%s",
        ",".join(str(r.get("kind") or "?") for r in _actions(args) if isinstance(r, dict))[:120],
        result.get("ok") if isinstance(result, dict) else None, changed, state["idle"], bool((args or {}).get("tab")),
        _safe_error(result) if failed else "",
    )
    if changed:
        state["page"] = page
        state["idle"] = 0
        return result
    if isinstance(result, dict) and any("already chosen" in str(w) for w in (result.get("warnings") or [])):
        return result  # the step had nothing to do: not a failed try
    key = f"{_action_key(tool, args)}:{before_page}"
    state["tries"][key] = state["tries"].get(key, 0) + 1
    state["idle"] += 1
    if not isinstance(result, dict):
        return result
    if state["idle"] >= NO_CHANGE_STOP:
        log.warning("browser.guard stopped a stuck turn idle=%s", state["idle"])
        state["stuck"] = True
        result["ok"] = False
        result["stopped"] = True
        result["error"] = _stop_reply(state)["error"]
    elif state["idle"] >= NO_CHANGE_WARN:
        warnings = result.get("warnings") if isinstance(result.get("warnings"), list) else []
        result["warnings"] = [
            *warnings,
            f"The last {state['idle']} actions changed nothing on this page. Stop and rethink: read it again, or use another way.",
        ]
    return result
