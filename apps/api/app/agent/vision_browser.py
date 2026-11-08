"""Operator-style computer use: look at the screen, act like a person, look again.

The fast browser loop (fast_browser.py) reads the page's element tree. Some pages defeat that: canvas apps, custom
date pickers, sites that hide or delay their structure. This loop works from screenshots instead, with OpenAI's
computer tool (the main model, gpt-6-luna, supports it): the model sees the screen, returns clicks / typing / keys /
scrolls in screen coordinates, they run on the computer (computer_act), and the next screenshot goes back. It ends
when the model answers in words (done), a safety check needs the user, or the step budget runs out.

Same rules as the fast loop: values the user gave are {{placeholders}} substituted only when typing; never a final or
irreversible click unless the user asked for it; bot checks and safety checks go to the user. Every step is kept in
the run history with its screenshot. Runs on the cloud browser; the desktop app needs to report its screen scale first
(screen_scale in computer_observe) before its own screen can be driven.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from typing import Any

log = logging.getLogger(__name__)

MAX_STEPS = 25
_KEYS = {"ctrl": "Control", "control": "Control", "alt": "Alt", "option": "Alt", "shift": "Shift", "cmd": "Meta",
         "command": "Meta", "meta": "Meta", "super": "Meta", "win": "Meta", "enter": "Enter", "return": "Enter",
         "esc": "Escape", "escape": "Escape", "space": "Space", "tab": "Tab", "backspace": "Backspace",
         "delete": "Delete", "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
         "arrowup": "ArrowUp", "arrowdown": "ArrowDown", "arrowleft": "ArrowLeft", "arrowright": "ArrowRight",
         "pageup": "PageUp", "pagedown": "PageDown", "home": "Home", "end": "End"}

SYSTEM = """You operate a web browser on the user's behalf by looking at screenshots and using the computer tool.
Work like a careful person: read the screen, act, check the result in the next screenshot.
- Finish the goal yourself. Choose sensible options the goal leaves open (for example a well-known site). Do not ask
  the user how to use a page.
- Fill forms by their labels; pick from suggestion lists; for dates, open the calendar and click the day (or type the
  date if the field takes text). Check fields show what you meant before searching or submitting.
- Dismiss cookie banners and popups that block the page.
- Values the user provided appear as {{name}} placeholders: type the placeholder exactly; it is replaced on typing.
- %s
- Stop and say so in words if only a person can continue: a CAPTCHA or "are you human" check, a sign-in you have no
  details for, a one-time code, a payment.
- When the goal is met, reply in words with the answer or what is ready (briefly), not with another action."""
_FINAL_BLOCK = ("Never click a final or irreversible control (Pay, Buy, Place order, Book, Send, Transfer, Delete, "
                "Cancel subscription): stop and say what is ready for the user to confirm.")
_FINAL_ALLOW = "The goal includes the final action, so you may click it once, after checking everything is right."


def _key(combo: Any) -> str:
    parts = combo if isinstance(combo, list) else str(combo or "").replace("-", "+").split("+")
    return "+".join(_KEYS.get(str(p).strip().lower(), str(p).strip() if len(str(p).strip()) > 1 else str(p).strip().lower())
                    for p in parts if str(p).strip())


def _sub(text: str, values: dict[str, str]) -> str:
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values.get(m.group(1), m.group(0)), text or "")


def to_actions(model_actions: list, values: dict[str, str]) -> tuple[list[dict], list[str]]:
    """The computer tool's actions as computer_act steps (screen coordinates unchanged), and what was skipped."""
    out: list[dict] = []
    skipped: list[str] = []
    for a in model_actions or []:
        a = a if isinstance(a, dict) else (a.model_dump() if hasattr(a, "model_dump") else {})
        kind = str(a.get("type") or "").lower()
        x, y = a.get("x"), a.get("y")
        if kind == "click":
            button = str(a.get("button") or "left").lower()
            out.append({"kind": "rightclick" if button == "right" else "click", "x": x, "y": y})
        elif kind in ("double_click", "dblclick"):
            out.append({"kind": "dblclick", "x": x, "y": y})
        elif kind == "scroll":
            out.append({"kind": "scroll", "dx": int(a.get("scroll_x") or 0), "dy": int(a.get("scroll_y") or 0)})
        elif kind == "type":
            out.append({"kind": "type", "text": _sub(str(a.get("text") or ""), values)})
        elif kind in ("keypress", "key"):
            keys = a.get("keys") or [a.get("key")]
            combo = _key(keys) if isinstance(keys, list) and len(keys) > 1 else _key(keys[0] if keys else "Enter")
            out.append({"kind": "key", "key": combo})
        elif kind == "wait":
            out.append({"kind": "wait", "ms": int(a.get("ms") or 1000)})
        elif kind in ("screenshot", "move"):
            continue  # a fresh screenshot is sent after every batch; pointer moves change nothing here
        else:
            skipped.append(kind or "unknown")
    return out, skipped


async def _screen(ctx) -> tuple[dict, str]:
    """The current screen: the page state and a PNG data URL ("" when no screenshot could be taken)."""
    from app.agent.computer_harness import computer_slug, run_computer
    from connector_core import BinaryResult

    state = await run_computer(ctx, "computer_observe", {})
    state = state if isinstance(state, dict) else {"ok": False, "error": str(state)[:300]}
    if state.get("screenshot_b64"):
        return state, "data:image/png;base64," + str(state["screenshot_b64"])
    path = state.get("screenshot_path")
    slug = computer_slug(ctx)
    conn = ctx.connectors.get(slug) if slug and hasattr(ctx.connectors, "get") else None
    if path and conn is not None:
        try:
            blob = await conn.call("publish_file", {"path": path, "filename": "screen.png"})
            if isinstance(blob, BinaryResult) and blob.data:
                return state, "data:image/png;base64," + base64.b64encode(blob.data).decode()
        except Exception as exc:  # noqa: BLE001
            log.info("vision: screenshot fetch failed: %s", type(exc).__name__)
    return state, ""


def _text_of(resp: Any) -> str:
    chunks = []
    for item in getattr(resp, "output", None) or []:
        if getattr(item, "type", "") == "message":
            for c in getattr(item, "content", None) or []:
                if getattr(c, "text", None):
                    chunks.append(c.text)
    return " ".join(chunks).strip()


def _done_kind(text: str) -> str:
    """A worded answer is either the result or a stop for the user (bot check, sign-in, code, payment, final step)."""
    from app.agent.fast_browser import need_user_kind

    t = text or ""
    stop = re.search(r"captcha|are you (a )?human|verify (that )?you|bot check|sign in|log in|one[- ]time|"
                     r"verification code|payment|ready for (you|the user) to confirm|needs? (you|your)", t, re.I)
    return "need_user" if stop and need_user_kind(t) == "real" else "done"


async def computer_task(ctx, goal: str, values: dict | None = None, allow_final_action: bool = False,
                        max_steps: int = MAX_STEPS) -> dict:
    """Do ``goal`` on the computer's browser by sight. Never raises."""
    from app.agent import usage
    from app.agent.fast_browser import _log, _result, scrub
    from app.config import get_settings
    from app.llm import openai_client

    vals = {k: str(v) for k, v in (values or {}).items()}
    started = time.monotonic()
    state: dict = {}
    try:
        from app.desktop.client import prefer_local

        if prefer_local():
            return {"ok": False, "done": False, "fallback": True, "unsupported": "desktop",
                    "note": "Seeing and clicking on this computer's own screen needs the next desktop app version."}
        state, image = await _screen(ctx)
        if not image:
            return _result(state, vals, ok=False, done=False, fallback=True, note="No screenshot of the computer was available.")
        if state.get("bot_wall"):
            from app.agent.fast_browser import BOT_WALL_NEED

            return _result(state, vals, ok=False, done=False, need_user=BOT_WALL_NEED)
        model = str(getattr(get_settings(), "computer_use_model", "") or "").strip() or get_settings().openai_model
        tools = [{"type": "computer"}]
        instructions = SYSTEM % (_FINAL_ALLOW if allow_final_action else _FINAL_BLOCK)
        placeholders = ", ".join("{{%s}}" % k for k in vals) or "none"
        first = [{"role": "user", "content": [
            {"type": "input_text", "text": f"Goal: {goal}\nValues you may type as placeholders: {placeholders}\n"
                                           f"Current page: {state.get('url') or ''} {state.get('title') or ''}"},
            {"type": "input_image", "image_url": image},
        ]}]
        client = openai_client()
        resp = await client.responses.create(model=model, tools=tools, instructions=instructions, input=first,
                                             max_output_tokens=1200)
        usage.from_openai(model, getattr(resp, "usage", None))
        for step in range(max_steps):
            calls = [o for o in (getattr(resp, "output", None) or []) if getattr(o, "type", "") == "computer_call"]
            if not calls:
                text = _text_of(resp)
                if _done_kind(text) == "need_user":
                    await _log("Stopped: needs you", text[:300], False, state)
                    return _result(state, vals, ok=False, done=False, via="vision", need_user=scrub(text, vals)[:500],
                                   took_ms=int((time.monotonic() - started) * 1000))  # fmt: skip
                await _log("Done", text[:300], True, state)
                return _result(state, vals, ok=True, done=True, via="vision", result=scrub(text, vals)[:2000],
                               took_ms=int((time.monotonic() - started) * 1000))  # fmt: skip
            outputs = []
            for call in calls:
                checks = list(getattr(call, "pending_safety_checks", None) or [])
                if checks:
                    reason = "; ".join(str(getattr(c, "message", "") or getattr(c, "code", "")) for c in checks)[:300]
                    await _log("Stopped: safety check", reason, False, state)
                    return _result(state, vals, ok=False, done=False, via="vision",
                                   need_user=f"Paused for your OK: {reason or 'the page asked for something sensitive'}.")
                model_actions = list(getattr(call, "actions", None) or []) or ([call.action] if getattr(call, "action", None) else [])
                actions, skipped = to_actions(model_actions, vals)
                if actions:
                    from app.agent.computer_harness import run_computer

                    await run_computer(ctx, "computer_act", {"actions": actions})
                state, image = await _screen(ctx)
                await _log(_describe(actions, vals), f"step {step + 1} by sight" + (f"; skipped {', '.join(skipped)}" if skipped else ""),
                           True, state)  # fmt: skip
                if state.get("bot_wall"):
                    from app.agent.fast_browser import BOT_WALL_NEED

                    return _result(state, vals, ok=False, done=False, via="vision", need_user=BOT_WALL_NEED)
                if not image:
                    return _result(state, vals, ok=False, done=False, fallback=True, note="The screenshot stopped working.")
                outputs.append({"type": "computer_call_output", "call_id": call.call_id,
                                "output": {"type": "computer_screenshot", "image_url": image}})
            resp = await client.responses.create(model=model, tools=tools, instructions=instructions, input=outputs,
                                                 previous_response_id=resp.id, max_output_tokens=1200)
            usage.from_openai(model, getattr(resp, "usage", None))
        return _result(state, vals, ok=False, done=False, via="vision", fallback=True,
                       note=f"Stopped after {max_steps} steps without finishing.", took_ms=int((time.monotonic() - started) * 1000))
    except Exception as exc:  # noqa: BLE001 - never break the turn
        from app.agent import cancel as run_cancel

        if isinstance(exc, run_cancel.TurnCancelled):
            raise
        log.warning("vision task failed: %s", type(exc).__name__, exc_info=True)
        return {"ok": False, "done": False, "fallback": True, "via": "vision", "error": type(exc).__name__}


def _describe(actions: list[dict], values: dict[str, str]) -> str:
    from app.agent.fast_browser import scrub

    parts = []
    for a in actions[:6]:
        k = a.get("kind")
        if k in ("click", "dblclick", "rightclick"):
            parts.append(f"{k} at {a.get('x')},{a.get('y')}")
        elif k == "type":
            parts.append(f"type '{scrub(str(a.get('text') or ''), values)[:40]}'")
        elif k == "key":
            parts.append(f"press {a.get('key')}")
        else:
            parts.append(str(k))
    return ", ".join(parts) or "look at the screen"
