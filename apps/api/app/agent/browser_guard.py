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
PAGE_READS = {"browser_navigate", "browser_snapshot", "browse_open"}
NAV_TOOLS = {"browser_navigate", "browse_open"}
SAME_PAGE_OPENS = 3
MAX_FACT_PAGES = 5


def _safe_error(result: dict) -> str:
    """The error's cause for the log, without quoted text: a "no option" message repeats what was typed."""
    return re.sub(r'"[^"]*"', '"…"', str(result.get("error") or ""))[:160]


def _warned(result: Any) -> str:
    rows = result.get("warnings") if isinstance(result, dict) else None
    if not rows:
        return ""
    return "page_click" if any("through the page" in str(w) for w in rows) else "no_effect"


def _targets(actions: list, elements: list) -> str:
    """What each step acted on, by control name and what it belongs to. Clicks only: a field's typed text is never logged."""
    names = {str(e.get("ref")): e for e in elements if isinstance(e, dict)}
    out = []
    for row in actions:
        if not isinstance(row, dict):
            continue
        el = names.get(str(row.get("ref") or "")) or {}
        name = str(el.get("name") or "?")[:40] if str(row.get("kind") or "") in {"click", "dblclick", "select", "pick"} else str(row.get("kind") or "?")
        near = f" ({str(el.get('near'))[:30]})" if el.get("near") else ""
        out.append(f"{row.get('ref') or ''}={name}{near}")
    return "; ".join(out)[:400]


def _carry_progress(state: dict, tool: str, args: dict | None, result: dict, acted_on: list) -> None:
    """Older page results get trimmed from the model's context, so each new one carries what the turn has done and the
    amounts it has seen. Without this the agent skipped steps and couldn't report totals it had already read."""
    from app.agent.fast_browser import note_page_facts

    note_page_facts(state, result, {})
    if tool in ACT_TOOLS and result.get("ok") is not False:
        line = _targets(_actions(args), acted_on)
        if line:
            state.setdefault("log", []).append(line[:200])
    seen = dict(list((state.get("seen") or {}).items())[-MAX_FACT_PAGES:])
    if state.get("log") or seen:
        result["so_far"] = {"steps": state.get("log", [])[-30:], "seen_on_pages": seen}


_STALE = re.compile(r"ref (?:not found: )?(e\d+)|ref not found: (e\d+)", re.I)
_STEP = re.compile(r"step (\d+) failed", re.I)


def stale_retry(ctx: Any, args: dict | None, result: Any, fresh: list) -> dict | None:
    """When a step's ref has gone because the page re-rendered, the steps still to run with their refs moved to the same
    controls on the fresh page. None unless every one is found exactly once, with its name and what it belongs to matching."""
    state = getattr(ctx, "_browser_guard", None)
    if not isinstance(state, dict) or not isinstance(result, dict) or result.get("ok") is not False:
        return None
    error = str(result.get("error") or "")
    if not _STALE.search(error) and "older page" not in error:
        return None
    rows = _actions(args)
    found = _STEP.search(error)
    start = int(found.group(1)) - 1 if found else int(result.get("completed") or 0)
    old = {str(e.get("ref")): e for e in state.get("acted_on") or [] if isinstance(e, dict)}
    moved = []
    for row in rows[start:]:
        if not isinstance(row, dict) or not row.get("ref"):
            moved.append(row)
            continue
        was = old.get(str(row["ref"]))
        if not was or not was.get("name"):
            return None
        same = [e for e in fresh if isinstance(e, dict) and e.get("name") == was.get("name") and e.get("role") == was.get("role") and e.get("near") == was.get("near")]
        if len(same) != 1:
            return None
        moved.append({**row, "ref": same[0].get("ref")})
    return {**(args or {}), "actions": moved} if moved else None


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
        state = {"page": "", "tries": {}, "idle": 0, "stuck": False, "elements": [], "once": set()}
        try:
            ctx._browser_guard = state
        except AttributeError:
            return None  # a context that takes no attributes simply runs unguarded
    return state


def final_step_done(ctx: Any) -> bool:
    """A run-once control (Finish, Place order) was clicked this turn: doing more could repeat it."""
    state = getattr(ctx, "_browser_guard", None)
    return bool(isinstance(state, dict) and state.get("once"))


def _once_labels(actions: list, elements: list) -> list[str]:
    from app.agent.fast_browser import once_labels

    return once_labels([a for a in actions if isinstance(a, dict)], {"elements": elements})


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
    if tool in NAV_TOOLS:
        state = _state(ctx)
        url = str((args or {}).get("url") or "")
        if state is not None and url:
            seen = state.setdefault("opened", {})
            seen[url] = seen.get(url, 0) + 1
            if seen[url] > SAME_PAGE_OPENS:
                log.warning("browser.guard refused to reopen a page opens=%s", seen[url])
                return {
                    "ok": False,
                    "repeated": True,
                    "error": (
                        f"Not opened: this address has been opened {seen[url] - 1} times with nothing done on it in between, and "
                        "opening it again won't show anything new. Work from what you've read, use browser_snapshot, or tell the user what you couldn't get."
                    ),
                }
        return None
    if tool not in ACT_TOOLS or _passive(args):
        return None
    state = _state(ctx)
    if state is None:
        return None
    if state["stuck"]:
        return _stop_reply(state)
    again = next((label for label in _once_labels(_actions(args), state["elements"]) if label.casefold() in state["once"]), "")
    if again:
        log.warning("browser.guard refused a second click on a run-once control")
        return {
            "ok": False,
            "already_done": True,
            "error": (
                f"Not run: '{again}' was already clicked in this turn, so that step has happened. Clicking it again could "
                "repeat an order or a send. Do not start the task over: report what was done."
            ),
        }
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
    if tool in ACT_TOOLS and isinstance(result, dict):
        rows = _actions(args)
        ran = rows if result.get("ok") is not False else rows[: int(result.get("completed") or 0)]
        # A declined pop-up means the click didn't take effect, so it can be tried again.
        declined = any(isinstance(d, dict) and d.get("answer") == "declined" for d in result.get("dialogs") or [])
        if not declined:
            state["once"].update(label.casefold() for label in _once_labels(ran, state["elements"]))
    acted_on = state["elements"]
    state["acted_on"] = acted_on
    if isinstance(result, dict) and tool not in ACT_TOOLS and result.get("url"):
        state["url"] = str(result["url"])
    if isinstance(result, dict) and isinstance(result.get("elements"), list):
        state["elements"] = result["elements"]
    if isinstance(result, dict) and tool in ACT_TOOLS | PAGE_READS:
        _carry_progress(state, tool, args, result, acted_on)
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
    if isinstance(result, dict) and result.get("url"):
        url = str(result["url"])
        was = str(state.get("url") or "")
        result["check"] = {"page_changed": changed, "url_changed": bool(was) and url != was}
        if was and url != was:
            result["navigated"] = {"from": was, "to": url}
    failed = isinstance(result, dict) and result.get("ok") is False
    log.info(
        "browser.step kinds=%s ok=%s changed=%s idle=%s tab=%s err=%s warned=%s on=%s",
        ",".join(str(r.get("kind") or "?") for r in _actions(args) if isinstance(r, dict))[:120],
        result.get("ok") if isinstance(result, dict) else None, changed, state["idle"], bool((args or {}).get("tab")),
        _safe_error(result) if failed else "", _warned(result), _targets(_actions(args), acted_on),
    )
    if isinstance(result, dict) and result.get("url"):
        state["url"] = str(result["url"])
    if changed:
        state["page"] = page
        state["idle"] = 0
        state["opened"] = {}  # work happened, so going back to a page is ordinary (a list and its detail pages)
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
