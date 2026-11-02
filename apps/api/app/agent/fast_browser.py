"""Fast browser automation (docs/BROWSING.md "Fast browser", docs/LLD.md §7.8).

Low-latency paths on top of the existing browser tools. Each one sits behind its own setting and hands back
to the normal screenshot / browser_act loop on any error:

- ``for_model``: the model reads the numbered element tree (``- e3 textbox Email [email]``), not the 150-element
  JSON list and screenshot paths (BROWSER_COMPACT_SNAPSHOT).
- ``browser_task``: a small fast model (BROWSER_FAST_MODEL, default OPENAI_MODEL) plans a batch of actions per
  call; the daemon runs them and stops at the first failure. After repeated failures it escalates to
  BROWSER_ESCALATE_MODEL, then returns ``fallback`` so the main agent continues itself (BROWSER_FAST_LOOP).
- Traces: a finished browser_task is saved as selector steps (``browser_traces``) and replayed next time with
  no model call per step; each step is verified and a mismatch falls back to the model (BROWSER_REPLAY).
- ``browser_download``: fetch a file link with the browser session's cookies (BROWSER_DIRECT_DOWNLOADS).
- ``prewarm``: start (or check) the browser as soon as a turn is classified as computer work (BROWSER_PREWARM).

Values the user gave (email, password, OTP) travel as ``{{name}}`` placeholders: the fast model sees only the
names, code substitutes them right before the daemon types, and they are never logged or stored. A secret
field in a saved trace keeps only its parameter name.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any
from urllib.parse import urlparse

from app.config import get_settings

log = logging.getLogger(__name__)

ALLOWED_KINDS = {
    "click", "dblclick", "fill", "type", "select", "pick", "check", "uncheck", "hover", "press", "scroll",
    "wait", "wait_for", "download", "back", "forward", "reload", "navigate",
}  # fmt: skip
MAX_BATCH = 12
PLAN_TIMEOUT_S = 45.0
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.-]{1,40})\s*\}\}")
# Final or irreversible controls the fast loop never clicks unless the caller allowed the final action.
_FINAL = re.compile(
    r"\b(pay|pay now|buy|buy now|place (?:your )?order|purchase|complete (?:purchase|order|booking)|confirm (?:and )?pay|"
    r"book now|confirm booking|reserve now|delete|remove account|close account|send money|transfer|cancel (?:my )?subscription)\b",
    re.I,
)
_SECRET_NAME = re.compile(r"pass|pwd|otp|code|pin|cvv|cvc|secret|token", re.I)


def setting(name: str, default: Any = True) -> Any:
    return getattr(get_settings(), name, default)


def fast_model() -> str:
    s = get_settings()
    return (str(getattr(s, "browser_fast_model", "") or "").strip()) or s.openai_model


def escalate_model() -> str:
    s = get_settings()
    return (str(getattr(s, "browser_escalate_model", "") or "").strip()) or s.openai_model


# --- what the model sees -----------------------------------------------------------------------------------------
def for_model(result: Any) -> Any:
    """Tool output for the model. Drops internal fields (trace, per-step timing detail) and, with
    BROWSER_COMPACT_SNAPSHOT, the elements list and screenshot fields when the numbered tree is present."""
    if not isinstance(result, dict):
        return result
    out = dict(result)
    out.pop("trace", None)
    timing = out.pop("timing", None)
    if isinstance(timing, dict) and timing.get("total_ms") is not None:
        out["took_ms"] = timing.get("total_ms")
    if setting("browser_compact_snapshot", True) and out.get("tree"):
        for key in ("elements", "viewport", "screenshot_path", "screenshot_name"):
            out.pop(key, None)
    return out


def page_brief(state: dict | None, text_chars: int = 3000, tree_chars: int = 7000) -> str:
    """The page as the fast model reads it: URL, title, alerts, a text excerpt and the numbered element tree."""
    st = state or {}
    parts = [f"URL: {st.get('url') or ''}", f"Title: {st.get('title') or ''}"]
    alerts = (st.get("verified") or {}).get("alerts") or st.get("alerts") or []
    if alerts:
        parts.append("Alerts: " + " | ".join(str(a)[:160] for a in alerts[:6]))
    if st.get("bot_wall"):
        parts.append("The page is a bot check / CAPTCHA.")
    text = str(st.get("text") or "")
    parts.append("Text:\n" + text[:text_chars])
    tree = str(st.get("tree") or "")
    parts.append("Elements:\n" + (tree[:tree_chars] or "(none)"))
    return "\n".join(parts)


# --- values and secrets ------------------------------------------------------------------------------------------
def fill_values(actions: list[dict], values: dict[str, str]) -> tuple[list[dict], list[str]]:
    """Replace ``{{name}}`` placeholders in step text with the caller's values. Returns (steps, missing names)."""
    missing: list[str] = []
    out: list[dict] = []
    for step in actions:
        row = dict(step)
        for key in ("text", "value", "url"):
            raw = row.get(key)
            if not isinstance(raw, str) or "{{" not in raw:
                continue

            def sub(m: re.Match) -> str:
                name = m.group(1)
                if name in values:
                    return str(values[name])
                missing.append(name)
                return m.group(0)

            row[key] = _PLACEHOLDER.sub(sub, raw)
        out.append(row)
    return out, sorted(set(missing))


def scrub(text: str, values: dict[str, str]) -> str:
    """Remove any caller value from text bound for logs or the model (a page may echo what was typed)."""
    out = str(text or "")
    for value in values.values():
        v = str(value or "")
        if len(v) >= 3:
            out = out.replace(v, "•••")
    return out


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")[:30]


def param_name(target: dict | None, taken: set[str]) -> str:
    t = target or {}
    for key in ("autocomplete", "label", "placeholder", "aria_label", "name_attr", "id", "name"):
        base = _slug(t.get(key) or "")
        if base and base not in {"on", "off", "text", "input"}:
            break
    else:
        base = "password" if str(t.get("input_type") or "") == "password" else "field"
    name, n = base, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    taken.add(name)
    return name


def trace_steps(planned: list[dict], daemon_trace: list[dict], values: dict[str, str]) -> list[dict]:
    """Saved-trace steps for the completed part of a batch: stable target descriptors instead of refs, and
    parameters instead of anything typed into a secret field (or anything that came from a caller value)."""
    taken: set[str] = set()
    steps: list[dict] = []
    by_index = {int(t.get("i", -1)): t for t in daemon_trace or [] if isinstance(t, dict)}
    for i, step in enumerate(planned):
        done = by_index.get(i)
        if done is None:
            break
        kind = str(step.get("kind") or "").lower()
        row: dict[str, Any] = {"kind": kind}
        target = done.get("target") if isinstance(done.get("target"), dict) else {}
        if target:
            row["target"] = target
        raw = step.get("text") if isinstance(step.get("text"), str) else step.get("value")
        if kind in {"fill", "type", "select", "pick"}:
            raw = str(raw or "")
            m = _PLACEHOLDER.fullmatch(raw.strip())
            secret = bool(done.get("secret")) or (m is not None and bool(_SECRET_NAME.search(m.group(1))))
            if m:
                row["param"] = m.group(1)
                taken.add(m.group(1))
            elif secret or raw in values.values():
                row["param"] = param_name(target, taken)
            else:
                row["text"] = raw[:500]
            if secret:
                row["secret"] = True
        elif kind in {"press", "key"}:
            row["text"] = str(step.get("text") or step.get("key") or "Enter")[:40]
        elif kind == "scroll":
            row["dy"] = int(step.get("dy") or 800)
        elif kind in {"wait", "wait_for"}:
            row["ms"] = int(step.get("wait_ms") or step.get("ms") or 1000)
            if kind == "wait_for" and step.get("text") and not _PLACEHOLDER.search(str(step.get("text"))):
                row["text"] = str(step["text"])[:120]
        if not target and kind not in {"press", "key", "scroll", "wait", "back", "forward", "reload"}:
            continue  # nothing stable to find it by next time
        steps.append(row)
    return steps


def trace_params(steps: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for step in steps:
        name = step.get("param")
        if name and name not in seen:
            seen[name] = {"name": name, "secret": bool(step.get("secret"))}
    return list(seen.values())


def replay_steps(steps: list[dict], values: dict[str, str]) -> tuple[list[dict], list[str]]:
    """Saved steps -> daemon replay steps with parameters filled in. Returns (steps, missing parameter names)."""
    out: list[dict] = []
    missing: list[str] = []
    for step in steps:
        row = {k: v for k, v in step.items() if k != "param"}
        name = step.get("param")
        if name:
            if name in values and values[name] is not None:
                row["text"] = str(values[name])
            else:
                missing.append(name)
                continue
        if row.get("kind") in {"wait", "wait_for"} and row.get("ms"):
            row["wait_ms"] = row.pop("ms")
        out.append(row)
    return out, sorted(set(missing))


# --- trace store -------------------------------------------------------------------------------------------------
def host_of(url: str) -> str:
    try:
        host = (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


_STOP = {"the", "and", "for", "with", "into", "from", "this", "that", "then", "my", "me", "on", "to", "in", "of", "a", "an", "at"}


def goal_key(goal: str) -> str:
    text = _PLACEHOLDER.sub(" ", str(goal or "").lower())
    text = re.sub(r"https?://\S+", " ", text)
    return " ".join(re.findall(r"[a-z0-9]+", text))[:200]


def similarity(a: str, b: str) -> float:
    ta = {w for w in goal_key(a).split() if len(w) > 1 and w not in _STOP}
    tb = {w for w in goal_key(b).split() if len(w) > 1 and w not in _STOP}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class TraceStore:
    """browser_traces in Postgres. Every method is best effort: errors are logged and swallowed."""

    async def save(self, user_id, *, host: str, name: str, goal: str, start_url: str, steps: list[dict],
                   source: str = "task", skill_id: str = "") -> str | None:  # fmt: skip
        try:
            from sqlalchemy import select

            from app.db import SessionLocal
            from app.models import BrowserTrace

            uid = uuid.UUID(str(user_id))
            async with SessionLocal() as db:
                q = select(BrowserTrace).where(BrowserTrace.user_id == uid, BrowserTrace.host == host, BrowserTrace.name == name)
                row = (await db.execute(q)).scalars().first()
                if row is None:
                    row = BrowserTrace(user_id=uid, host=host, name=name)
                    db.add(row)
                row.goal, row.start_url, row.steps, row.params = goal[:2000], start_url[:2000], steps, trace_params(steps)
                row.source, row.skill_id = source, skill_id
                await db.commit()
                return str(row.id)
        except Exception as exc:  # noqa: BLE001
            log.info("browser trace save skipped: %s", type(exc).__name__)
            return None

    async def candidates(self, user_id, *, host: str = "", trace_id: str = "", skill_id: str = "") -> list[dict]:
        try:
            from sqlalchemy import select

            from app.db import SessionLocal
            from app.models import BrowserTrace

            uid = uuid.UUID(str(user_id))
            q = select(BrowserTrace).where(BrowserTrace.user_id == uid)
            if trace_id:
                q = q.where(BrowserTrace.id == uuid.UUID(str(trace_id)))
            elif skill_id:
                q = q.where(BrowserTrace.skill_id == str(skill_id))
            elif host:
                q = q.where(BrowserTrace.host == host)
            q = q.order_by(BrowserTrace.updated_at.desc()).limit(50)
            async with SessionLocal() as db:
                rows = (await db.execute(q)).scalars().all()
            return [
                {"id": str(r.id), "host": r.host, "name": r.name, "goal": r.goal, "start_url": r.start_url,
                 "steps": list(r.steps or []), "params": list(r.params or []), "source": r.source, "skill_id": r.skill_id,
                 "uses": r.uses, "successes": r.successes, "failures": r.failures}  # fmt: skip
                for r in rows
            ]
        except Exception as exc:  # noqa: BLE001
            log.info("browser trace lookup skipped: %s", type(exc).__name__)
            return []

    async def mark(self, trace_id: str, ok: bool) -> None:
        try:
            from app.db import SessionLocal
            from app.models import BrowserTrace

            async with SessionLocal() as db:
                row = await db.get(BrowserTrace, uuid.UUID(str(trace_id)))
                if row is None:
                    return
                row.uses = (row.uses or 0) + 1
                if ok:
                    row.successes = (row.successes or 0) + 1
                else:
                    row.failures = (row.failures or 0) + 1
                await db.commit()
        except Exception as exc:  # noqa: BLE001
            log.info("browser trace mark skipped: %s", type(exc).__name__)


STORE: TraceStore = TraceStore()


def pick_trace(rows: list[dict], *, name: str = "", goal: str = "") -> dict | None:
    """Best saved trace: exact name, else the most similar goal (>= 0.6). Traces that keep failing are skipped."""
    usable = [r for r in rows if int(r.get("failures") or 0) <= int(r.get("successes") or 0) + 2 and r.get("steps")]
    want = goal_key(name)
    if want:
        for row in usable:
            if goal_key(row.get("name") or "") == want:
                return row
    best, score = None, 0.0
    for row in usable:
        sc = max(similarity(goal or name, row.get("goal") or ""), similarity(goal or name, row.get("name") or ""))
        if sc > score:
            best, score = row, sc
    return best if score >= 0.6 else None


# --- calls into the computer -------------------------------------------------------------------------------------
async def _internal(ctx, name: str, args: dict) -> Any:
    """Computer tools that are not in the connector's catalog (browser_replay, browser_download, browser_rec,
    browser_warm): straight to the connector, like git_tools' read_bytes. Stop still ends the run here."""
    from app.agent import cancel as run_cancel
    from app.agent import computer_harness as computer

    run_cancel.check()
    slug = computer.computer_slug(ctx)
    if not slug:
        return computer.computer_missing()
    if getattr(ctx, "call_impl", None) is not None:
        return await ctx.call_impl(slug, name, args)
    from app.connectors.runtime import RuntimeConnector

    return await RuntimeConnector(ctx.connectors[slug]).call(name, args)


async def _act(ctx, actions: list[dict]) -> dict:
    from app.agent import computer_harness as computer

    out = await computer.run_computer(ctx, "browser_act", {"actions": actions})
    return out if isinstance(out, dict) else {"ok": False, "error": str(out)[:300]}


async def _navigate(ctx, url: str) -> dict:
    from app.agent import computer_harness as computer

    out = await computer.run_computer(ctx, "browser_navigate", {"url": url})
    return out if isinstance(out, dict) else {"ok": False, "error": str(out)[:300]}


async def _snapshot(ctx) -> dict:
    from app.agent import computer_harness as computer

    out = await computer.run_computer(ctx, "browser_snapshot", {})
    return out if isinstance(out, dict) else {"ok": False, "error": str(out)[:300]}


def _blocked(state: dict) -> bool:
    return bool(state.get("bot_wall") or state.get("handoff") or state.get("computer_unavailable") or state.get("status") == "awaiting_user")


# --- replay ------------------------------------------------------------------------------------------------------
async def replay(ctx, trace: dict, values: dict[str, str]) -> dict:
    """Run a saved trace without model calls. Navigate steps go through browser_navigate; the rest through the
    daemon's verified replay. Returns {ok, state, completed, mismatch?, missing?}."""
    steps, missing = replay_steps(list(trace.get("steps") or []), values)
    if missing:
        return {"ok": False, "missing": missing, "completed": 0}
    started = time.monotonic()
    state: dict = {}
    if trace.get("start_url") and not (steps and steps[0].get("kind") == "navigate"):
        state = await _navigate(ctx, str(trace["start_url"]))
        if state.get("ok") is False or _blocked(state):
            return {"ok": False, "state": state, "completed": 0, "error": state.get("error") or "start page did not load"}
    done = 0
    segment: list[dict] = []

    async def flush() -> dict | None:
        nonlocal done, segment, state
        if not segment:
            return None
        res = await _internal(ctx, "browser_replay", {"steps": segment})
        res = res if isinstance(res, dict) else {"ok": False, "error": str(res)[:300]}
        if res.get("downloads"):
            from app.agent import computer_harness as computer

            res = await computer._publish_downloads(ctx, res)
        state = res
        done += int(res.get("completed") or 0)
        segment = []
        if res.get("ok") is False:
            return {"ok": False, "state": res, "completed": done, "mismatch": bool(res.get("mismatch")), "error": res.get("error")}
        return None

    for step in steps:
        if step.get("kind") == "navigate":
            failed = await flush()
            if failed:
                return failed
            state = await _navigate(ctx, str(step.get("url") or ""))
            if state.get("ok") is False or _blocked(state):
                return {"ok": False, "state": state, "completed": done, "error": state.get("error") or "page did not load"}
            done += 1
            continue
        segment.append(step)
    failed = await flush()
    if failed:
        return failed
    log.info("browser.replay trace=%s steps=%d ms=%d", trace.get("id"), done, int((time.monotonic() - started) * 1000))
    return {"ok": True, "state": state, "completed": done}


# --- the fast loop -----------------------------------------------------------------------------------------------
FAST_SYSTEM = """You drive a web browser for an AI assistant, quickly and carefully.
Each turn you get the goal, the current page (URL, title, alerts, a text excerpt and numbered interactive elements such as "- e3 textbox Email [email]") and what happened last turn.
Reply with a JSON object only: {"actions": [...], "done": false, "result": "", "need_user": ""}.
- actions: every step you can already predict, in order, in ONE batch, e.g. [{"kind":"fill","ref":"e3","text":"{{email}}"},{"kind":"fill","ref":"e4","text":"{{password}}"},{"kind":"click","ref":"e5"}].
  kinds: click, fill, type, select (text = option to pick; also for type-ahead lists), check, uncheck, press (text = key), scroll, wait_for (text), download (ref of a file link), back, navigate (url).
  Use only refs from the current Elements list. End the batch at a step whose result you must see first (a type-ahead suggestion list, a page that loads new fields, a new page).
- Values the user provided are placeholders: write {{name}} exactly; never type a real value yourself and never invent personal data. Available placeholders: %s
- done=true when the goal is met (or the information asked for is on the page): put the proof or the requested data in result, briefly.
- need_user: a short reason when only the user can continue (CAPTCHA, a one-time code or value you do not have, an ambiguous choice).
- %s
- Dismiss cookie banners and popups that block the page. Never navigate away from the site unless the goal needs it."""

_FINAL_RULE_BLOCK = (
    "Never click a final or irreversible control (Pay, Buy, Place order, Book, Send, Transfer, Delete, cancel a "
    "subscription): stop with done=true and say what is ready for the user to confirm."
)
_FINAL_RULE_ALLOW = "The goal includes finishing the final action, so you may click it once everything is filled and checked."


def parse_plan(raw: str) -> dict | None:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        data = json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None
    actions = data.get("actions") or []
    if not isinstance(actions, list):
        actions = []
    clean = []
    for step in actions:
        if len(clean) >= MAX_BATCH:
            break
        if isinstance(step, dict) and str(step.get("kind") or "").lower() in ALLOWED_KINDS:
            clean.append({k: v for k, v in step.items() if k in {"kind", "ref", "text", "value", "url", "dy", "wait_ms", "ms"}})
    return {
        "actions": clean,
        "done": bool(data.get("done")),
        "result": str(data.get("result") or "")[:2000],
        "need_user": str(data.get("need_user") or "")[:500],
    }


async def plan_step(model: str, goal: str, state: dict, values: dict[str, str], last: str, allow_final: bool) -> dict | None:
    from app.llm import chat_complete

    names = ", ".join(f"{{{{{k}}}}}" for k in values) or "(none)"
    system = FAST_SYSTEM % (names, _FINAL_RULE_ALLOW if allow_final else _FINAL_RULE_BLOCK)
    user = (
        f"Goal: {goal}\n\nLast turn: {last or 'start'}\n\n{scrub(page_brief(state), values)}\n\n"
        'Reply with the JSON object {"actions": [...], "done": ..., "result": ..., "need_user": ...}.'
    )
    resp = await asyncio.wait_for(
        chat_complete(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            max_tokens=900,
            model=model,
        ),
        timeout=PLAN_TIMEOUT_S,
    )
    return parse_plan(resp.choices[0].message.content or "")


def final_click(actions: list[dict], state: dict) -> str:
    """Name of a final/irreversible control the batch would click, else ""."""
    names = {str(e.get("ref")): str(e.get("name") or "") for e in state.get("elements") or [] if isinstance(e, dict)}
    for step in actions:
        if str(step.get("kind") or "").lower() in {"click", "dblclick", "press"}:
            label = names.get(str(step.get("ref") or ""), "")
            if label and _FINAL.search(label):
                return label
    return ""


def _result(state: dict, values: dict[str, str], **fields: Any) -> dict:
    page = for_model(state) if isinstance(state, dict) else {}
    if isinstance(page, dict):
        page = dict(page)
        page.pop("elements", None)
        if page.get("text"):
            page["text"] = scrub(str(page["text"]), values)[:6000]
    return {**fields, "page": page}


async def browser_task(ctx, goal: str, url: str = "", values: dict | None = None, max_steps: int = 0,
                       allow_final_action: bool = False, use_saved: bool = True) -> dict:  # fmt: skip
    """See the module docstring. Never raises: problems come back as ``fallback: true`` with the page."""
    vals = {str(k): str(v) for k, v in (values or {}).items() if v is not None}
    goal = str(goal or "").strip()
    started = time.monotonic()
    log_steps: list[dict] = []
    limit = max(1, min(int(max_steps or setting("browser_fast_max_steps", 12) or 12), 25))
    user_id = getattr(getattr(ctx, "user", None), "id", None)
    try:
        state: dict = {}
        target_host = host_of(url)
        # 1. A saved run of the same task: replay it with no model calls.
        if setting("browser_replay", True) and use_saved and user_id is not None:
            if not target_host:
                state = await _snapshot(ctx)
                target_host = host_of(str(state.get("url") or ""))
            rows = await STORE.candidates(user_id, host=target_host) if target_host else []
            trace = pick_trace(rows, goal=goal)
            if trace is not None:
                t0 = time.monotonic()
                rep = await replay(ctx, trace, vals)
                await STORE.mark(trace["id"], bool(rep.get("ok")))
                log_steps.append({"via": "replay", "ok": bool(rep.get("ok")), "steps": rep.get("completed"), "ms": int((time.monotonic() - t0) * 1000)})
                if rep.get("ok"):
                    return _result(rep.get("state") or {}, vals, ok=True, done=True, via="replay", trace=trace.get("name"),
                                   result="Replayed the saved steps; check the page below.", log=log_steps,
                                   took_ms=int((time.monotonic() - started) * 1000))  # fmt: skip
                if rep.get("state"):
                    state = rep["state"]
        # 2. The model loop.
        recorded: list[dict] = []
        start_url = url
        if url and (not state or host_of(str(state.get("url") or "")) != target_host):
            state = await _navigate(ctx, url)
            recorded.append({"kind": "navigate", "url": url})
        if not state:
            state = await _snapshot(ctx)
        if not start_url:
            start_url = str(state.get("url") or "")
        if _blocked(state):
            return _result(state, vals, ok=False, done=False, need_user="The page needs a person (bot check or hand-off).", log=log_steps)
        model, strong = fast_model(), escalate_model()
        escalated = model == strong
        failures, last = 0, ""
        for i in range(limit):
            t0 = time.monotonic()
            try:
                plan = await plan_step(model, goal, state, vals, last, allow_final_action)
            except Exception as exc:  # noqa: BLE001 - model error: escalate or fall back
                log.info("browser.task plan failed model=%s: %s", model, type(exc).__name__)
                plan = None
            plan_ms = int((time.monotonic() - t0) * 1000)
            if plan is None or (not plan["actions"] and not plan["done"] and not plan["need_user"]):
                failures += 1
                last = "Your last reply was not a usable JSON plan. Reply with the JSON object only."
                log_steps.append({"step": i + 1, "model": model, "plan_ms": plan_ms, "ok": False, "why": "no plan"})
            elif plan["need_user"]:
                return _result(state, vals, ok=False, done=False, need_user=plan["need_user"], log=log_steps,
                               took_ms=int((time.monotonic() - started) * 1000))  # fmt: skip
            elif plan["done"] and not plan["actions"]:
                trace_id = await _save(user_id, goal, start_url, recorded, vals)
                log_steps.append({"step": i + 1, "model": model, "plan_ms": plan_ms, "done": True})
                return _result(state, vals, ok=True, done=True, via="model", result=scrub(plan["result"], vals),
                               saved_trace=bool(trace_id), log=log_steps, took_ms=int((time.monotonic() - started) * 1000))  # fmt: skip
            else:
                actions = plan["actions"]
                blocked = "" if allow_final_action else final_click(actions, state)
                if blocked:
                    return _result(state, vals, ok=True, done=False, stopped_before=blocked,
                                   result=f"Stopped before '{blocked}' (a final action). Ask the user, then finish with browser_act.",
                                   log=log_steps, took_ms=int((time.monotonic() - started) * 1000))  # fmt: skip
                t1 = time.monotonic()
                state, ok, err, kept = await _run_batch(ctx, actions, vals)
                recorded.extend(kept)
                act_ms = int((time.monotonic() - t1) * 1000)
                log_steps.append({"step": i + 1, "model": model, "plan_ms": plan_ms, "act_ms": act_ms, "actions": [a.get("kind") for a in actions], "ok": ok})
                log.info(
                    "browser.task step=%d model=%s plan_ms=%d act_ms=%d actions=%s ok=%s",
                    i + 1, model, plan_ms, act_ms, ",".join(str(a.get("kind")) for a in actions), ok,
                )  # fmt: skip
                if _blocked(state):
                    return _result(state, vals, ok=False, done=False, need_user="The page needs a person (bot check or hand-off).", log=log_steps)
                if isinstance(err, list):
                    return _result(state, vals, ok=False, done=False, need_user="Missing values: " + ", ".join(err), log=log_steps)
                if ok:
                    last = f"Ran {len(actions)} action(s): {', '.join(str(a.get('kind')) for a in actions)}. All worked."
                    if plan["done"]:
                        trace_id = await _save(user_id, goal, start_url, recorded, vals)
                        return _result(state, vals, ok=True, done=True, via="model", result=scrub(plan["result"], vals),
                                       saved_trace=bool(trace_id), log=log_steps, took_ms=int((time.monotonic() - started) * 1000))  # fmt: skip
                    continue
                failures += 1
                last = scrub(f"A step failed: {err}. Use the refs in the fresh page below.", vals)
            if failures >= 2:
                if not escalated:
                    model, escalated, failures = strong, True, 0
                    log.info("browser.task escalating to model=%s", model)
                    continue
                break
        return _result(state, vals, ok=False, done=False, fallback=True,
                       note="The fast loop did not finish. Continue yourself with browser_act from this page.",
                       log=log_steps, took_ms=int((time.monotonic() - started) * 1000))  # fmt: skip
    except Exception as exc:  # noqa: BLE001 - never break the turn; the main agent continues
        from app.agent import cancel as run_cancel

        if isinstance(exc, run_cancel.TurnCancelled):
            raise
        log.warning("browser.task failed: %s: %s", type(exc).__name__, scrub(str(exc), vals)[:200])
        return {"ok": False, "fallback": True, "error": f"{type(exc).__name__}", "note": "Use browser_snapshot / browser_act.", "log": log_steps}


async def _run_batch(ctx, actions: list[dict], values: dict[str, str]) -> tuple[dict, bool, Any, list[dict]]:
    """Run one planned batch; navigate steps go through browser_navigate. Returns (state, ok, error, trace steps)."""
    kept: list[dict] = []
    state: dict = {}
    chunk: list[dict] = []

    async def run_chunk() -> tuple[bool, Any]:
        nonlocal state, chunk
        if not chunk:
            return True, None
        filled, missing = fill_values(chunk, values)
        if missing:
            return False, missing
        state = await _act(ctx, filled)
        done = int(state.get("completed") or 0)
        kept.extend(trace_steps(chunk[:done], state.get("trace") or [], values))
        chunk = []
        return state.get("ok") is not False, state.get("error")

    for step in actions:
        if str(step.get("kind")).lower() == "navigate":
            ok, err = await run_chunk()
            if not ok:
                return state, False, err, kept
            filled, missing = fill_values([step], values)
            if missing:
                return state, False, missing, kept
            state = await _navigate(ctx, str(filled[0].get("url") or ""))
            if state.get("ok") is False:
                return state, False, state.get("error") or "page did not load", kept
            if not _PLACEHOLDER.search(str(step.get("url") or "")):
                kept.append({"kind": "navigate", "url": str(step.get("url") or "")})
            continue
        chunk.append(step)
    ok, err = await run_chunk()
    return state, ok, err, kept


async def _save(user_id, goal: str, start_url: str, recorded: list[dict], values: dict[str, str]) -> str | None:
    if not setting("browser_replay", True) or user_id is None or not recorded:
        return None
    host = host_of(start_url)
    steps = [dict(step) for step in recorded]
    if steps and steps[0].get("kind") == "navigate" and steps[0].get("url") == start_url:
        steps = steps[1:]  # replay opens start_url itself
    if not host or not steps:
        return None  # nothing beyond opening the page
    # Never store a literal that equals a caller value.
    secret_values = {v for v in values.values() if v}
    for step in steps:
        if step.get("text") in secret_values:
            step.pop("text", None)
            step["param"] = step.get("param") or "value"
    return await STORE.save(user_id, host=host, name=goal_key(goal)[:200], goal=goal, start_url=start_url, steps=steps)


# --- direct downloads --------------------------------------------------------------------------------------------
async def browser_download(ctx, url: str = "", ref: str = "", filename: str = "") -> dict:
    """Fetch a file with the session cookies (no click-through); with a ref, the daemon's download step tries the
    direct fetch first and falls back to clicking and capturing the browser download."""
    from app.agent import computer_harness as computer

    if url and setting("browser_direct_downloads", True):
        res = await _internal(ctx, "browser_download", {"url": url, "filename": filename})
        if isinstance(res, dict) and res.get("ok") is not False and res.get("downloads"):
            return await computer._publish_downloads(ctx, res)
        if not ref:
            return res if isinstance(res, dict) else {"ok": False, "error": str(res)[:300]}
    if ref:
        return await computer.run_computer(ctx, "browser_act", {"actions": [{"kind": "download", "ref": ref, **({"url": url} if url else {})}]})
    return {"ok": False, "error": "browser_download needs url or ref"}


# --- warm browser ------------------------------------------------------------------------------------------------
_WARM: dict[str, float] = {}
_WARM_TASKS: set = set()
WARM_EVERY_S = 120.0


def prewarm(ctx) -> None:
    """Start the browser daemon (and restore saved cookies) in the background while the model plans its first
    step. BROWSER_PREWARM: computer (boot if needed), warm (only when the computer is already up), off."""
    mode = str(setting("browser_prewarm", "computer") or "").strip().lower()
    if mode in {"", "off", "0", "false", "no"}:
        return
    from app.agent import computer_harness as computer

    slug = computer.computer_slug(ctx)
    user = str(getattr(getattr(ctx, "user", None), "id", "") or "")
    if not slug or not user or getattr(ctx, "call_impl", None) is not None:
        return
    now = time.monotonic()
    if now - _WARM.get(user, -1e9) < WARM_EVERY_S:
        return
    _WARM[user] = now

    async def run() -> None:
        t0 = time.monotonic()
        try:
            from app.connectors.runtime import RuntimeConnector

            out = await RuntimeConnector(ctx.connectors[slug]).call("browser_warm", {"only_if_running": mode == "warm"})
            log.info("browser.prewarm ok=%s ms=%d", (out or {}).get("ok"), int((time.monotonic() - t0) * 1000))
        except Exception as exc:  # noqa: BLE001
            log.info("browser.prewarm skipped: %s", type(exc).__name__)

    try:
        task = asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        return
    _WARM_TASKS.add(task)
    task.add_done_callback(_WARM_TASKS.discard)


# --- teach mode: DOM recording -> trace --------------------------------------------------------------------------
def dom_events_to_steps(events: list[dict]) -> tuple[str, list[dict]]:
    """Recorder events -> (start url, replay steps). Consecutive edits of one field keep the last; secret fields
    become parameters; navigations caused by a click or Enter are implied, any other page change (an address the
    user typed) becomes a navigate step."""
    steps: list[dict] = []
    start_url = last_url = ""
    taken: set[str] = set()
    for ev in events or []:
        if not isinstance(ev, dict) or str(ev.get("kind") or "") not in {"click", "fill", "select", "check", "uncheck", "press"}:
            continue
        url = str(ev.get("url") or "").split("#", 1)[0]
        if url.startswith(("http://", "https://")):
            if not start_url:
                start_url = url
            elif url != last_url and steps and steps[-1].get("kind") not in {"click", "press"}:
                steps.append({"kind": "navigate", "url": url})
            last_url = url
        target = ev.get("target") if isinstance(ev.get("target"), dict) else {}
        target = {k: str(v)[:200] for k, v in target.items() if v not in (None, "")}
        row: dict[str, Any] = {"kind": ev["kind"], "target": target}
        if ev["kind"] == "fill":
            prev = steps[-1] if steps else None
            if prev and prev.get("kind") == "fill" and prev.get("target") == target:
                steps.pop()
            if ev.get("secret"):
                row["param"], row["secret"] = param_name(target, taken), True
            else:
                row["text"] = str(ev.get("text") or "")[:500]
        elif ev["kind"] == "select":
            row["text"] = str(ev.get("text") or "")[:200]
        elif ev["kind"] == "press":
            row["text"] = str(ev.get("text") or "Enter")[:40]
        if ev["kind"] == "click" and steps and steps[-1].get("kind") == "click" and steps[-1].get("target") == target:
            continue  # double-recorded click
        steps.append(row)
    return start_url, steps[:200]


async def replay_named(ctx, name: str = "", url: str = "", skill_id: str = "", values: dict | None = None) -> dict:
    """browser_replay: run a saved flow (a finished browser_task, or a taught skill's DOM recording) by name, site
    or skill id, without model calls. On a mismatch it stops and returns the page so the caller continues."""
    vals = {str(k): str(v) for k, v in (values or {}).items() if v is not None}
    user_id = getattr(getattr(ctx, "user", None), "id", None)
    if user_id is None:
        return {"ok": False, "error": "no user for saved runs"}
    host = host_of(url)
    rows = await STORE.candidates(user_id, host=host, skill_id=skill_id)
    trace = pick_trace(rows, name=name, goal=name) if name else (rows[0] if rows else None)
    if trace is None:
        return {
            "ok": False,
            "error": "no saved run matches",
            "saved": [{"name": r.get("name"), "host": r.get("host"), "params": r.get("params")} for r in rows[:10]],
            "hint": "Use browser_task (it saves the run when it finishes) or browser_act.",
        }
    started = time.monotonic()
    rep = await replay(ctx, trace, vals)
    if rep.get("missing"):
        return {"ok": False, "missing_values": rep["missing"], "params": trace.get("params"), "hint": "Pass them in values."}
    await STORE.mark(trace["id"], bool(rep.get("ok")))
    fields: dict[str, Any] = {"ok": bool(rep.get("ok")), "via": "replay", "trace": trace.get("name"), "steps": rep.get("completed"),
                              "took_ms": int((time.monotonic() - started) * 1000)}  # fmt: skip
    if not rep.get("ok"):
        fields.update(mismatch=bool(rep.get("mismatch")), error=scrub(str(rep.get("error") or ""), vals),
                      hint="The site changed. Continue from this page with browser_task (same goal) or browser_act.")  # fmt: skip
    return _result(rep.get("state") or {}, vals, **fields)


async def save_teach_trace(user_id, skill_id: str, name: str, events: list[dict]) -> str | None:
    """Teach mode: store the DOM recording of a taught skill as a replayable trace."""
    start_url, steps = dom_events_to_steps(events)
    host = host_of(start_url)
    if not host or not steps:
        return None
    label = f"{goal_key(name)[:150]} [skill {str(skill_id)[:8]}]".strip()
    return await STORE.save(user_id, host=host, name=label, goal=name or "",
                            start_url=start_url, steps=steps, source="teach", skill_id=str(skill_id))  # fmt: skip


def prompt_hint() -> str:
    """System-prompt lines for the fast browser tools that are switched on (empty when all are off)."""
    bits = []
    if setting("browser_fast_loop", True):
        bits.append(
            "Routine website work with a clear goal (log in, fill a known form, search a site, open a statement or download): "
            "call browser_task first with the goal, start url and the values it needs (email, password, …) — it plans several "
            "actions per call on the page's elements with a fast model and replays a saved run when one exists. If it returns "
            "fallback, need_user or stopped_before, continue yourself from the page it returns. "
        )
    if setting("browser_replay", True):
        bits.append("A taught skill or a task done before on that site: browser_replay (skill_id, or name/url) runs the saved steps without model calls. ")
    if setting("browser_direct_downloads", True):
        bits.append("To get a file whose link is on the page, browser_download (url or ref) fetches it directly with the signed-in session. ")
    bits.append(
        "When you browser_act yourself, batch the predictable actions of one step in one call (fill email, fill password, click Sign in); "
        "it stops at the first failure and returns the new page. "
    )
    return "".join(bits)


def teach_replay_line(skill_id: str) -> str:
    return (f"\n\nFast path: the browser steps of this recording are saved. Run them first with "
            f"browser_replay(skill_id=\"{skill_id}\", values={{...}} for any password or code), then continue from the "
            f"returned page if it stops on a mismatch.")  # fmt: skip
