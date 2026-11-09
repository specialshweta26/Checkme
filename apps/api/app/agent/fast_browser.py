"""Fast browser automation (docs/BROWSING.md "Fast browser", docs/LLD.md §7.8).

Low-latency paths on top of the existing browser tools. Each one sits behind its own setting and hands back
to the normal screenshot / browser_act loop on any error:

- ``for_model``: the model reads the numbered element tree (``- e3 textbox Email [email]``), not the 150-element
  JSON list and screenshot paths (BROWSER_COMPACT_SNAPSHOT).
- ``browser_task``: a small fast model (BROWSER_FAST_MODEL, default OPENAI_SMALL_MODEL, then OPENAI_MODEL) plans a batch of actions per
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
from app.connectors.computer_daemon import looks_secret, secret_text

log = logging.getLogger(__name__)

ALLOWED_KINDS = {
    "click", "dblclick", "fill", "type", "select", "pick", "check", "uncheck", "hover", "press", "scroll",
    "wait", "wait_for", "download", "back", "forward", "reload", "navigate", "find", "pick_date",
}  # fmt: skip
MAX_BATCH = 12
PLAN_TIMEOUT_S = 45.0
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.-]{1,40})\s*\}\}")
# Final or irreversible controls the fast loop never clicks unless the caller allowed the final action.
_FINAL = re.compile(
    r"\b(pay|pay now|buy|buy now|place (?:your )?order|purchase|complete (?:purchase|order|booking)|confirm (?:and )?pay|"
    r"book now|book (?:flights?|tickets?|hotel|room|appointment|table|seats?)|confirm booking|reserve now|delete|remove account|"
    r"close account|send(?! (?:(?:a|an|the|me|my) )*(?:code|otp|link|verification|again|reset))|transfer|make payment|"
    r"proceed to pay(?:ment)?|submit (?:order|payment)|confirm (?:order|payment|purchase)|confirm & pay|"
    r"cancel (?:my )?(?:subscription|order|booking))\b",
    re.I,
)
# Devanagari vowel signs are not word characters, so these labels are matched without word edges.
_FINAL_HI = re.compile("भुगतान|खरीद|ऑर्डर कर|बुक कर|भेजें|भेजिए|हटाएं|हटाएँ|मिटाएं|रद्द कर")
# Words that only mean "final" on a payment step: "Proceed" there can charge the user, on a sign-in page it cannot.
_GENERIC_ADVANCE = re.compile(r"\s*(?:proceed\b.*|continue|next|submit|confirm|ok|okay|yes|done|agree|i agree|finish)\W*", re.I)
_PAYMENT_PAGE = re.compile(
    r"\b(?:cvv|cvc|card number|upi id|net ?banking|amount payable|total payable|amount to pay|payment (?:method|details|summary))\b|भुगतान",
    re.I,
)
_PAYMENT_URL = re.compile(r"/(?:pay|payments?)(?:[/?#]|$)", re.I)


def setting(name: str, default: Any = True) -> Any:
    return getattr(get_settings(), name, default)


def fast_model() -> str:
    from app.llm import small_model

    return (str(getattr(get_settings(), "browser_fast_model", "") or "").strip()) or small_model()


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
    composer = str(st.get("composer") or "").strip()
    if composer:
        parts.append("Composer still holds: " + composer[:300])
    text = str(st.get("text") or "")
    if len(text) > text_chars:
        half = max(1, text_chars // 2)
        text = text[:half] + "\n…\n" + text[-half:]
    parts.append("Text:\n" + text)
    tree = str(st.get("tree") or "")
    omitted = int(st.get("elements_omitted") or 0)
    if omitted and len(tree) > tree_chars:
        tree = tree[:tree_chars] + f"\n… {omitted} more elements are not listed (use find)."
    parts.append("Elements:\n" + (tree[:tree_chars + 80] or "(none)"))
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


def secret_param(name: str) -> bool:
    """A {{placeholder}} holding a secret. A bare `code` is a sign-in code; `promo_code` and `zip_code` are not."""
    return looks_secret(name) or secret_text(name) == "code"


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
        if kind in {"fill", "type", "select", "pick", "pick_date"}:
            raw = str(raw or "")
            m = _PLACEHOLDER.fullmatch(raw.strip())
            secret = bool(done.get("secret")) or (m is not None and secret_param(m.group(1)))
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


def field_label(target: dict | None) -> str:
    t = target or {}
    for key in ("label", "placeholder", "aria_label", "name", "name_attr", "id"):
        if t.get(key):
            return " ".join(str(t[key]).split())[:80]
    return ""


def make_variables(steps: list[dict]) -> list[dict]:
    """Saved-run steps where every value typed or picked in a form field is a variable: {{name}} named after the field,
    with the field's label and the recorded value as its example (docs/BROWSING.md "Variables"). Secret fields already
    keep only a name. The run then takes new values each time, filled from what the user writes."""
    taken = {str(s.get("param")) for s in steps if s.get("param")}
    out = []
    for step in steps:
        row = dict(step)
        kind = str(row.get("kind") or "").lower()
        text = row.get("text")
        if kind in {"fill", "type", "select", "pick", "pick_date"} and not row.get("param") and isinstance(text, str) and text.strip() and not row.get("secret"):
            target = row.get("target") if isinstance(row.get("target"), dict) else {}
            row["param"] = param_name(target, taken)
            row["example"] = text[:200]
            label = field_label(target)
            if label:
                row["label"] = label
            row.pop("text", None)
        out.append(row)
    return out


def trace_params(steps: list[dict]) -> list[dict]:
    """The values a saved run needs: step parameters (with a label and example when known), and {{names}} written in
    AI steps."""
    seen: dict[str, dict] = {}
    for step in steps:
        names = [step.get("param")] if step.get("param") else []
        if str(step.get("kind") or "") == "goal":
            names += _PLACEHOLDER.findall(str(step.get("text") or ""))
        for name in names:
            if name and name not in seen:
                row = {"name": name, "secret": bool(step.get("secret")) or secret_param(name)}
                if step.get("param") == name:
                    if step.get("label"):
                        row["label"] = str(step["label"])
                    if step.get("example") and not row["secret"]:
                        row["example"] = str(step["example"])
                seen[name] = row
    return list(seen.values())


def _changed_values(steps: list[dict], values: dict[str, str]) -> dict[str, tuple[str, str]]:
    """{recorded example: (new value, field label)} for the variables this run gets a different value for."""
    changed: dict[str, tuple[str, str]] = {}
    for st in steps:
        name, example = st.get("param"), st.get("example")
        if not name or not example or st.get("secret"):
            continue
        new = values.get(name)
        if new not in (None, "") and str(new).strip().lower() != str(example).strip().lower():
            changed[str(example)] = (str(new), str(st.get("label") or name))
    return changed


def _mentions(hay: str, example: str) -> bool:
    """Does a recorded element's text refer to a recorded value: the same words, or the same date written another way."""
    h, e = hay.lower(), example.strip().lower()
    if len(e) >= 3 and e in h:
        return True
    first = e.split(",")[0].strip()
    if len(first) >= 4 and first in h:
        return True
    try:
        from app.connectors.computer_daemon import parse_date

        day = parse_date(example)
        return day is not None and any(parse_date(part) == day for part in (hay, *hay.split("|")) if part.strip())
    except Exception:  # noqa: BLE001
        return False


def adapt_to_values(steps: list[dict], values: dict[str, str]) -> tuple[list[dict], bool]:
    """A saved run used with new values: a recorded click on an old value (the suggestion "Bengaluru", the calendar
    day "Fri Dec 19 2026") would pick the old value again, so it becomes an AI step that picks the new one. Returns
    (steps, whether any value changed)."""
    changed = _changed_values(steps, values)
    if not changed:
        return steps, False
    out: list[dict] = []
    for st in steps:
        kind = str(st.get("kind") or "").lower()
        target = st.get("target") if isinstance(st.get("target"), dict) else {}
        if kind in {"click", "dblclick", "select", "pick", "check"} and not st.get("param") and target:
            hay = " | ".join(str(target.get(k) or "") for k in ("name", "aria_label", "label", "text", "title"))
            hit = next(((old, new) for old, new in changed.items() if _mentions(hay, old)), None)
            if hit:
                old, (new, label) = hit
                out.append({"kind": "goal", "text": f"Choose “{new}” for {label} (the recorded run picked “{old}” here)"})
                continue
        out.append(st)
    return out, True


def bare_start(url: str) -> str:
    """The start address without its query: a recorded results page carries the old search in it."""
    parts = urlparse(url)
    return parts._replace(query="", fragment="").geturl() if parts.query else url


def replay_steps(steps: list[dict], values: dict[str, str]) -> tuple[list[dict], list[str]]:
    """Saved steps -> daemon replay steps with parameters filled in. Returns (steps, missing parameter names)."""
    out: list[dict] = []
    missing: list[str] = []
    for step in steps:
        row = {k: v for k, v in step.items() if k not in {"param", "example", "label"}}
        if row.get("kind") in {"goal", "check"}:
            # An AI step keeps its {{names}}: the fast loop sees names, code types the values.
            missing.extend(n for n in _PLACEHOLDER.findall(str(row.get("text") or "")) if values.get(n) is None)
            out.append(row)
            continue
        name = step.get("param")
        if name:
            if name in values and values[name] is not None:
                row["text"] = str(values[name])
            elif step.get("example") and not step.get("secret"):
                row["text"] = str(step["example"])  # not given this time: the value it was recorded with
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
                steps = make_variables(steps)
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
                 "steps": make_variables(list(r.steps or [])), "params": trace_params(make_variables(list(r.steps or []))),
                 "source": r.source, "skill_id": r.skill_id,
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


    async def propose(self, trace_id: str, steps: list[dict], note: list[dict]) -> bool:
        """Keep a fix found while replaying. Nothing the replay uses changes until ``accept``."""
        try:
            from datetime import datetime, timezone

            from app.db import SessionLocal
            from app.models import BrowserTrace

            async with SessionLocal() as db:
                row = await db.get(BrowserTrace, uuid.UUID(str(trace_id)))
                if row is None:
                    return False
                row.proposed_steps, row.proposed_note = steps, note
                row.proposed_at = datetime.now(timezone.utc)
                await db.commit()
                return True
        except Exception as exc:  # noqa: BLE001
            log.info("browser trace propose skipped: %s", type(exc).__name__)
            return False

    async def _owned(self, db, user_id, trace_id: str):
        from app.models import BrowserTrace

        try:
            row = await db.get(BrowserTrace, uuid.UUID(str(trace_id)))
        except (ValueError, TypeError):
            return None
        if row is None or str(row.user_id) != str(user_id):
            return None
        return row

    async def resolve(self, user_id, trace_id: str, accept: bool) -> dict:
        """Accept (the fix becomes the saved steps; the old ones are kept for one undo) or reject a proposed fix."""
        from app.db import SessionLocal

        async with SessionLocal() as db:
            row = await self._owned(db, user_id, trace_id)
            if row is None:
                return {"ok": False, "error": "no such saved run"}
            if not row.proposed_steps:
                return {"ok": False, "error": "this saved run has no proposed fix"}
            if accept:
                row.prior_steps, row.steps = list(row.steps or []), make_variables(list(row.proposed_steps))
                row.params = trace_params(row.steps)
                row.failures = 0
            row.proposed_steps, row.proposed_note, row.proposed_at = None, None, None
            await db.commit()
            return {"ok": True, "accepted": accept, "id": str(row.id), "name": row.name, "steps": len(row.steps or [])}

    async def edit(self, user_id, trace_id: str, items: list[dict], name: str = "") -> dict:
        """Rewrite a saved run: ``items`` is the new step list, each {"keep": i} (saved step i, unchanged) or
        {"goal": "sentence"} (an AI step the model does each run). The old steps are kept for one undo, and a
        pending fix is dropped (it was made for the old steps)."""
        from app.db import SessionLocal

        async with SessionLocal() as db:
            row = await self._owned(db, user_id, trace_id)
            if row is None:
                return {"ok": False, "error": "no such saved run"}
            old = list(row.steps or [])
            try:
                new = build_steps(old, items)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            row.prior_steps, row.steps = old, new
            row.params = trace_params(new)
            if name.strip():
                row.name = name.strip()[:200]
            row.proposed_steps, row.proposed_note, row.proposed_at = None, None, None
            await db.commit()
            return {"ok": True, "run": describe_trace(row)}

    async def listing(self, user_id, trace_id: str = "") -> list[dict]:
        """The user's saved runs as describe_trace rows, newest first (one row when trace_id is given)."""
        from sqlalchemy import select

        from app.db import SessionLocal
        from app.models import BrowserTrace

        async with SessionLocal() as db:
            if trace_id:
                row = await self._owned(db, user_id, trace_id)
                return [describe_trace(row)] if row is not None else []
            q = select(BrowserTrace).where(BrowserTrace.user_id == uuid.UUID(str(user_id)))
            rows = (await db.execute(q.order_by(BrowserTrace.updated_at.desc()).limit(200))).scalars().all()
            return [describe_trace(r) for r in rows]

    async def delete(self, user_id, trace_id: str) -> bool:
        from app.db import SessionLocal

        async with SessionLocal() as db:
            row = await self._owned(db, user_id, trace_id)
            if row is None:
                return False
            await db.delete(row)
            await db.commit()
            return True

    async def undo(self, user_id, trace_id: str) -> dict:
        """Put back the steps an accepted fix replaced."""
        from app.db import SessionLocal

        async with SessionLocal() as db:
            row = await self._owned(db, user_id, trace_id)
            if row is None:
                return {"ok": False, "error": "no such saved run"}
            if not row.prior_steps:
                return {"ok": False, "error": "nothing to undo"}
            row.steps, row.prior_steps = list(row.prior_steps), None
            row.params = trace_params(row.steps)
            await db.commit()
            return {"ok": True, "id": str(row.id), "name": row.name, "steps": len(row.steps or [])}


MAX_STEPS = 200
MAX_GOAL_CHARS = 600


def build_steps(old: list[dict], items: list[dict]) -> list[dict]:
    """New saved steps from an edit list (see TraceStore.edit). Raises ValueError for a bad item."""
    if not isinstance(items, list) or not items:
        raise ValueError("a saved run needs at least one step")
    if len(items) > MAX_STEPS:
        raise ValueError(f"at most {MAX_STEPS} steps")
    out: list[dict] = []
    for n, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError(f"step {n} is not an object")
        if "keep" in item:
            try:
                i = int(item["keep"])
            except (TypeError, ValueError):
                raise ValueError(f"step {n}: keep must be a step number") from None
            if not 0 <= i < len(old):
                raise ValueError(f"step {n}: there is no saved step {i + 1}")
            out.append(dict(old[i]))
        elif "check" in item:
            text = " ".join(str(item.get("check") or "").split())
            if not text:
                raise ValueError(f"step {n}: write the yes/no question to check on the page")
            row = {"kind": "check", "text": text[:MAX_GOAL_CHARS]}
            for branch in ("yes", "no"):
                raw = item.get(branch)
                try:
                    target = int(raw) if raw not in (None, "") else n + 1
                except (TypeError, ValueError):
                    raise ValueError(f"step {n}: {branch} must be a step number or 0 for the end") from None
                if target < 0 or target > len(items) or target == n:
                    raise ValueError(f"step {n}: {branch} goes to step {target}, which isn't another step")
                row[branch] = target
            out.append(row)
        elif "approve" in item:
            text = " ".join(str(item.get("approve") or "").split())
            out.append({"kind": "approve", "text": (text or "Check the page, then continue?")[:MAX_GOAL_CHARS]})
        elif "goal" in item:
            text = " ".join(str(item.get("goal") or "").split())
            if not text:
                raise ValueError(f"step {n}: write what the step should do")
            out.append({"kind": "goal", "text": text[:MAX_GOAL_CHARS]})
        else:
            raise ValueError(f"step {n}: use keep, goal, approve or check")
    return out


def describe_trace(row) -> dict:
    """A saved run as the user sees it: plain-English steps, stats, and any fix waiting for approval."""
    proposed = list(row.proposed_steps or []) if row.proposed_steps else []
    return {
        "id": str(row.id), "name": row.name, "goal": row.goal, "host": row.host, "start_url": row.start_url,
        "source": row.source, "skill_id": row.skill_id, "params": list(row.params or []),
        "steps": [describe_step(s) for s in row.steps or []],
        "step_rows": [{"index": i, "text": describe_step(s), "ai": str(s.get("kind") or "") == "goal",
                       "approve": str(s.get("kind") or "") == "approve",
                       **({"check": {"text": s.get("text"), "yes": s.get("yes"), "no": s.get("no")}} if s.get("kind") == "check" else {}),
                       **({"variable": s.get("param"), "label": s.get("label")} if s.get("param") else {})}
                      for i, s in enumerate(row.steps or [])],
        "has_run_key": bool(getattr(row, "run_key_hash", "")),
        "uses": row.uses or 0, "successes": row.successes or 0, "failures": row.failures or 0,
        "proposed_fix": {"steps": [describe_step(s) for s in proposed], "changes": list(row.proposed_note or []),
                         "at": row.proposed_at.isoformat() if row.proposed_at else None} if proposed else None,
        "can_undo": bool(row.prior_steps),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }  # fmt: skip


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


async def _snapshot(ctx, find: str = "") -> dict:
    from app.agent import computer_harness as computer

    out = await computer.run_computer(ctx, "browser_snapshot", {"find": find[:120]} if find.strip() else {})
    return out if isinstance(out, dict) else {"ok": False, "error": str(out)[:300]}


# A page that comes back with no elements and almost no text is usually still loading (heavy single-page sites
# render late). Look again before letting the planner decide anything about it.
SETTLE_WAITS_S = (1.5, 3.0, 5.0)


def page_empty(state: dict | None) -> bool:
    """Nothing to act on yet: no element tree, no elements and (almost) no text."""
    st = state or {}
    return (not str(st.get("tree") or "").strip() and not st.get("elements")
            and len(str(st.get("text") or "").strip()) < 20)


async def settle(ctx, state: dict) -> dict:
    """Re-read an empty page a few times (about 10 s in all) while it finishes loading."""
    for delay in SETTLE_WAITS_S:
        if not page_empty(state) or _blocked(state):
            return state
        await asyncio.sleep(delay)
        try:
            fresh = await _snapshot(ctx)
        except Exception:  # noqa: BLE001 - keep the page we have; the planner can still wait or navigate
            return state
        if isinstance(fresh, dict) and fresh.get("ok") is not False:
            state = fresh
    return state


# "Needs you" ends a run, so it must mean a step only a person can take. Planners also use it to ask for things they
# can do or decide themselves; those are turned back into instructions and the run continues.
_SITE_CHOICE = re.compile(r"\b(ok(ay)?|fine|happy) with (using|me using)\b|\bwhich (site|website|airline|app|service|ota)\b|"
                          r"\b(another|a different|preferred?) (site|website|airline|ota)\b", re.I)
_REAL_HUMAN = re.compile(r"captcha|robot|are you (a )?human|verify (that )?you|bot check|sign ?in|log ?in|password|"
                         r"\botp\b|one[- ]time|verification code|2fa|two[- ]factor|payment|card (details|number)|"
                         r"\bpay\b|purchase|confirm (the )?(booking|order|payment|purchase)|which (one|of (these|the))|"
                         r"choose between|your (preference|choice) (of|between)", re.I)
_PAGE_ASK = re.compile(r"element|\burl\b|title|visible text|page (text|content|details)|screenshot|snapshot|"
                       r"(page|site|form|results?) (to )?(load|loads|loading|appear)|still loading|not loaded|"
                       r"date picker|calendar|dropdown|click|select .* (in|from) the|interact", re.I)


def need_user_kind(text: str) -> str:
    """real (only a person can continue), site_choice (the planner can pick a site) or page (it can look again)."""
    t = text or ""
    if _SITE_CHOICE.search(t):
        return "site_choice"
    if _REAL_HUMAN.search(t):
        return "real"
    if _PAGE_ASK.search(t):
        return "page"
    return "real"


def _blocked(state: dict) -> bool:
    return bool(state.get("bot_wall") or state.get("handoff") or state.get("computer_unavailable") or state.get("status") == "awaiting_user")


# --- replay ------------------------------------------------------------------------------------------------------
# --- healing saved runs ------------------------------------------------------------------------------------------
MAX_HEALS = 3
HEAL_ROUNDS = 4


def describe_step(step: dict) -> str:
    """One saved step in plain English, e.g. 'click the button "Reports"' or 'fill the textbox "Email" with {{email}}'."""
    kind = str(step.get("kind") or "").lower()
    if kind == "navigate":
        return f"open {step.get('url') or 'the page'}"
    if kind == "goal":
        return str(step.get("text") or "").strip()
    if kind == "approve":
        return "Wait for your OK: " + (str(step.get("text") or "").strip() or "continue?")
    if kind == "check":
        def goto(n: Any) -> str:
            return f"step {int(n)}" if n else "the end"

        return f"Check: {str(step.get('text') or '').strip()} (yes → {goto(step.get('yes'))}, no → {goto(step.get('no'))})"
    t = step.get("target") if isinstance(step.get("target"), dict) else {}
    label = next((str(t[k]) for k in ("name", "aria_label", "label", "placeholder", "testid", "name_attr", "id") if t.get(k)), "")
    role = str(t.get("role") or t.get("tag") or "element")
    what = f'the {role} "{label[:60]}"' if label else f"the {role}"
    value = "{{%s}}" % step["param"] if step.get("param") else f'"{str(step.get("text") or "")[:60]}"'
    if kind in {"fill", "type"}:
        return f"fill {what} with {value}" + (" and press Enter" if kind == "type" else "")
    if kind in {"select", "pick"}:
        return f"choose {value} in {what}"
    if kind in {"press", "key"}:
        return f"press {step.get('text') or 'Enter'}"
    if kind == "scroll":
        return "scroll the page"
    if kind in {"wait", "wait_for"}:
        return f'wait for "{step.get("text")}"' if step.get("text") else "wait"
    if kind in {"check", "uncheck", "hover", "dblclick", "download", "upload"}:
        return f"{kind} {what}"
    return f"click {what}"


async def heal_step(ctx, trace: dict, step: dict, values: dict[str, str], allow_final: bool = False) -> list[dict] | None:
    """The site changed under a saved step: let the model do just that step on the current page and return the
    steps it took (saved form), or None. Nothing is saved here; the caller proposes the fix."""
    intent = describe_step(step)
    goal = (
        f'A saved run of "{trace.get("goal") or trace.get("name") or "a task"}" reached a step whose page changed. '
        f"Do only this step on the current page, then reply done: {intent}. If the page needs something first for it "
        "(open the menu that holds it, close a popup), do that too. Do not do any later step."
    )
    out = await browser_task(ctx, goal, values=values, max_steps=HEAL_ROUNDS, allow_final_action=allow_final,
                             use_saved=False, save=False)  # fmt: skip
    steps = out.get("_recorded") if isinstance(out, dict) else None
    if not (isinstance(out, dict) and out.get("ok") and out.get("done") and steps):
        return None
    return [dict(s) for s in steps]


GOAL_ROUNDS = 6
MAX_VISITS = 3
FILL_SYSTEM = (
    "A saved web task has fields to fill. From the user's request, give each field's value. Reply with JSON only: "
    '{"values": {"<field name>": "<value>" or null}}. Each field has a label (what the form calls it) and an example '
    "(a value it was used with before): use them to know what the field means and to write the value in the same "
    "format (a date like the example's date, a city or airport like the example). Work out relative dates from today's "
    "date. Never invent a value: null when the request doesn't say."
)


async def fill_variables(params: list[dict], text: str, given: dict[str, str]) -> dict[str, str]:
    """Values for a saved run's variables taken from what the user wrote ("Mumbai to Deoghar on 21 Nov"), for the
    ones the caller did not pass. Secret ones are never filled this way. Best effort: {} on any problem."""
    wanted = [p for p in params or [] if p.get("name") and not p.get("secret") and p["name"] not in given]
    text = str(text or "").strip()
    if not wanted or not text:
        return {}
    from datetime import date

    from app.llm import chat_complete

    fields = [{"name": p["name"], "label": p.get("label") or "", "example": p.get("example") or ""} for p in wanted]
    user = (
        f"Today is {date.today().isoformat()}.\nUser's request:\n{text[:3000]}\n\nFields:\n"
        + json.dumps(fields, ensure_ascii=False)
        + "\n\nReply in json."
    )
    try:
        resp = await chat_complete([{"role": "system", "content": FILL_SYSTEM}, {"role": "user", "content": user}],
                                   response_format={"type": "json_object"}, max_tokens=400, model=fast_model())  # fmt: skip
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception as exc:  # noqa: BLE001
        log.info("variables not filled: %s", type(exc).__name__)
        return {}
    values = data.get("values") if isinstance(data, dict) else None
    names = {f["name"] for f in fields}
    out = {}
    for name, value in (values or {}).items() if isinstance(values, dict) else []:
        if name in names and value not in (None, "") and len(str(value)) <= 500:
            out[name] = str(value).strip()
    return out


async def add_filled_variables(ctx, trace: dict, vals: dict[str, str], extra_text: str = "") -> dict[str, str]:
    """Fill a saved run's missing variables from the user's message (and the task text). Returns what was filled."""
    if not setting("browser_fill_variables", True):
        return {}
    text = "\n".join(t for t in (str(getattr(ctx, "query", "") or ""), extra_text) if t.strip())
    filled = await fill_variables(list(trace.get("params") or []), text, vals)
    for key, value in filled.items():
        vals.setdefault(key, value)
    return filled
CHECK_SYSTEM = (
    "You answer one yes/no question about a web page for an automation. Read the page and reply with JSON only: "
    '{"answer": "yes" or "no", "why": "a few words"}. If the page does not show it, answer no.'
)


async def check_page(ctx, question: str, state: dict | None) -> bool:
    """A check step: ask the fast model a yes/no question about the current page. Anything unclear is "no"."""
    from app.llm import chat_complete

    page = state if isinstance(state, dict) and (state.get("tree") or state.get("text")) else await _snapshot(ctx)
    try:
        resp = await chat_complete(
            [{"role": "system", "content": CHECK_SYSTEM}, {"role": "user", "content": f"Question: {question}\n\n{page_brief(page, 3000, 4000)}\n\nReply in json."}],
            response_format={"type": "json_object"}, max_tokens=200, model=fast_model(),
        )  # fmt: skip
        data = json.loads(resp.choices[0].message.content or "{}")
        return str(data.get("answer") or "").strip().lower().startswith("y")
    except Exception as exc:  # noqa: BLE001
        log.info("check step answered no: %s", type(exc).__name__)
        return False


async def run_goal_step(ctx, trace: dict, step: dict, values: dict[str, str], allow_final: bool = False) -> dict:
    """Run one AI step of a saved run (a sentence the user wrote) with the fast loop. Returns {ok, state, error?}."""
    text = str(step.get("text") or "").strip()
    if not text:
        return {"ok": True}
    goal = (
        f'Part of the saved run "{trace.get("goal") or trace.get("name") or "a task"}". Do only this on the current '
        f"page, then reply done with what you see: {text}"
    )
    out = await browser_task(ctx, goal, values=values, max_steps=GOAL_ROUNDS, allow_final_action=allow_final,
                             use_saved=False, save=False)  # fmt: skip
    ok = bool(isinstance(out, dict) and out.get("ok") and out.get("done"))
    page = out.get("page") if isinstance(out, dict) else None
    error = "" if ok else str((out or {}).get("need_user") or (out or {}).get("stopped_before") or (out or {}).get("note") or "did not finish")
    return {"ok": ok, "state": page if isinstance(page, dict) else {}, "error": f"AI step '{text[:80]}': {error}" if error else ""}


async def replay(ctx, trace: dict, values: dict[str, str], heal: bool = False, allow_final: bool = False,
                 start_step: int = 1) -> dict:  # fmt: skip
    """Run a saved trace without model calls. Navigate steps go through browser_navigate; the rest through the
    daemon's verified replay. With ``heal``, a step whose element changed is redone by the model (only that step)
    and the replay carries on. Returns {ok, state, completed, mismatch?, missing?, healed?, steps?}: ``steps`` is
    the saved-form run with the fixes in, for a proposal."""
    raw, new_values = adapt_to_values([dict(s) for s in trace.get("steps") or []], values)
    steps, missing = replay_steps(raw, values)
    if missing:
        return {"ok": False, "missing": missing, "completed": 0}
    started = time.monotonic()
    state: dict = {}
    first = max(0, min(int(start_step or 1) - 1, len(steps)))  # resuming after an approval: the page is already there
    if first == 0 and trace.get("start_url") and not (steps and steps[0].get("kind") == "navigate"):
        start = bare_start(str(trace["start_url"])) if new_values else str(trace["start_url"])
        state = await _navigate(ctx, start)
        if state.get("ok") is False or _blocked(state):
            return {"ok": False, "state": state, "completed": 0, "error": state.get("error") or "start page did not load"}
    done = 0
    healed: list[dict] = []
    fixed: list[dict] = list(raw[:first])  # saved-form steps as they ran, fixes included
    i = first
    visits: dict[int, int] = {}
    while i < len(steps):
        visits[i] = visits.get(i, 0) + 1
        if visits[i] > MAX_VISITS:
            return {"ok": False, "state": state, "completed": done, "error": f"step {i + 1} ran {MAX_VISITS} times; the run seems to loop"}
        if steps[i].get("kind") == "check":
            answer = await check_page(ctx, str(raw[i].get("text") or ""), state)
            target = int(raw[i].get("yes" if answer else "no") or 0)
            await _log(f"Check: {raw[i].get('text')} → {'yes' if answer else 'no'}",
                       f"Going to {'step ' + str(target) if target else 'the end'}.", True, state, shot=False)  # fmt: skip
            fixed.append(raw[i])
            done += 1
            i = target - 1 if target > 0 else len(steps)
            continue
        if steps[i].get("kind") == "approve":
            if i == first and first > 0:
                # The user approved this step; carry on past it.
                fixed.append(raw[i])
                done, i = done + 1, i + 1
                continue
            text = str(raw[i].get("text") or "Continue?")
            await _log(f"Waiting for your OK: {text}", "The saved run has an approval step here.", True, state, shot=False)
            return {"ok": False, "state": state, "completed": done, "needs_approval": {"step": i + 1, "text": text},
                    "error": f"step {i + 1} waits for the user's OK"}  # fmt: skip
        if steps[i].get("kind") == "goal":
            # An AI step: the fast loop does this sentence on the current page, every run.
            out = await run_goal_step(ctx, trace, raw[i], values, allow_final)
            state = out.get("state") or state
            if not out.get("ok"):
                return {"ok": False, "state": state, "completed": done, "error": out.get("error") or "an AI step did not finish",
                        "ai_step": i + 1, **({"healed": healed} if healed else {})}  # fmt: skip
            fixed.append(raw[i])
            done, i = done + 1, i + 1
            continue
        if steps[i].get("kind") == "navigate":
            state = await _navigate(ctx, str(steps[i].get("url") or ""))
            if state.get("ok") is False or _blocked(state):
                return {"ok": False, "state": state, "completed": done, "error": state.get("error") or "page did not load"}
            fixed.append(raw[i])
            done, i = done + 1, i + 1
            continue
        j = i
        while j < len(steps) and steps[j].get("kind") not in {"navigate", "goal", "approve", "check"}:
            j += 1
        res = await _internal(ctx, "browser_replay", {"steps": steps[i:j]})
        res = res if isinstance(res, dict) else {"ok": False, "error": str(res)[:300]}
        if res.get("downloads"):
            from app.agent import computer_harness as computer

            res = await computer._publish_downloads(ctx, res)
        state = res
        completed = max(0, min(int(res.get("completed") or 0), j - i))
        fixed.extend(raw[i : i + completed])
        done += completed
        if res.get("ok") is not False:
            i = j
            continue
        bad = i + completed
        failed = {"ok": False, "state": res, "completed": done, "mismatch": bool(res.get("mismatch")), "error": res.get("error"),
                  **({"healed": healed} if healed else {})}  # fmt: skip
        if not heal or bad >= j or len(healed) >= MAX_HEALS or _blocked(res):
            return failed
        fix = await heal_step(ctx, trace, raw[bad], values, allow_final)
        if not fix:
            log.info("browser.replay heal failed trace=%s step=%d", trace.get("id"), bad + 1)
            return failed
        healed.append({"step": bad + 1, "was": describe_step(raw[bad]), "now": [describe_step(s) for s in fix],
                       "error": str(res.get("error") or "")[:200]})  # fmt: skip
        await _log(f"Fixed saved step {bad + 1}: {describe_step(raw[bad])} → " + ", then ".join(describe_step(s) for s in fix),
                   "The page changed, so this step was worked out again.", True, None, shot=False)  # fmt: skip
        log.info("browser.replay healed trace=%s step=%d with=%d", trace.get("id"), bad + 1, len(fix))
        fixed.extend(fix)
        done, i = done + 1, bad + 1
        state = await _snapshot(ctx)
    log.info("browser.replay trace=%s steps=%d healed=%d ms=%d", trace.get("id"), done, len(healed), int((time.monotonic() - started) * 1000))
    out: dict[str, Any] = {"ok": True, "state": state, "completed": done}
    if healed:
        out.update(healed=healed, steps=fixed)
    return out


async def _propose_fix(trace: dict, rep: dict) -> bool:
    """A replay that only finished thanks to fixes: keep them as a proposal for the user to accept."""
    if not (rep.get("ok") and rep.get("healed") and rep.get("steps") and trace.get("id")):
        return False
    return await STORE.propose(str(trace["id"]), list(rep["steps"]), list(rep["healed"]))


def heal_fields(rep: dict, proposed: bool) -> dict:
    """What the caller (and the user) should know about a healed replay."""
    if not rep.get("healed"):
        return {}
    changes = "; ".join(f"step {h['step']}: was '{h['was']}', now " + " then ".join(f"'{n}'" for n in h["now"]) for h in rep["healed"])
    note = f"The site changed, so this run fixed itself ({changes})."
    if proposed:
        note += (" The fix is saved as a proposal; the saved run is unchanged until the user accepts it. Tell the user "
                 "what changed and ask whether to keep the fix (browser_saved_runs action=accept_fix or reject_fix).")  # fmt: skip
    return {"healed": rep["healed"], "proposed_fix": proposed, "heal_note": note}


# --- the fast loop -----------------------------------------------------------------------------------------------
FAST_SYSTEM = """You drive a web browser for an AI assistant, quickly and carefully.
Each turn you get the goal, the current page (URL, title, alerts, a text excerpt and numbered interactive elements such as "- e3 textbox Email [email]") and what happened last turn.
Reply with a JSON object only: {"why": "", "actions": [...], "done": false, "result": "", "need_user": "", "notes": ""}.
- "Done so far" lists what this run has already done, in order. Never redo a listed step and never start the goal over: do only what is left. As soon as the last part of the goal is done, reply done=true. A page that looks like the start does not mean the work is undone.
- notes: facts the goal asks you to report that are on this page now and may be gone later (names, prices, totals, confirmation numbers). Everything in notes must also go in result when you finish.
- why: one short sentence for the user watching the run later: what you see on the page and why these actions.
- actions: every step you can already predict, in order, in ONE batch, e.g. [{"kind":"fill","ref":"e3","text":"{{email}}"},{"kind":"fill","ref":"e4","text":"{{password}}"},{"kind":"click","ref":"e5"}].
  kinds: click, fill, type, select (text = option to pick; also for type-ahead lists), check, uncheck, hover (menus that open on mouse-over), press (text = key), scroll, wait_for (text), download (ref of a file link), back, navigate (url), find (text = words to look for).
  Use only refs from the current Elements list. End the batch at a step whose result you must see first (a type-ahead suggestion list, a menu that opens, a page that loads new fields, a new page).
- The Elements list may end with "… N more elements are not listed". If what you need is not listed, send ONE action {"kind":"find","text":"words from its label"} to re-list the page with matching elements first (or scroll).
- Dates: use {"kind":"pick_date","ref":<the date field>,"text":"YYYY-MM-DD"}: it opens the field's calendar, moves to the month and clicks the day (or fills a date box). Never click calendar days or month arrows one by one. Before Apply / Submit, check the date fields show the dates you meant.
- Menus: if clicking a menu does not open it, hover it, then click the item in the next batch. When "Last turn" says a step may not have done anything, do not repeat the same step: try its label, its parent, hover, or another way.
- Values the user provided are placeholders: write {{name}} exactly; never type a real value yourself and never invent personal data. Available placeholders: %s
- done=true when the goal is met (or the information asked for is on the page): put the proof or the requested data in result, briefly.
- need_user: a short reason when only the user can continue (CAPTCHA, a sign-in, a one-time code or value you do not have, a payment, or a choice the goal leaves open between options you found). Never use it to ask for the page, its elements or URL (you have them; if the list is empty, wait_for or navigate again), how to use a control, or which site to use (pick a well-known one yourself).
- Fill each field by its label: places and airports go into From / To (Where from? / Where to?) and you pick the matching suggestion; dates go into date fields with pick_date only. After a batch, check the fields show what you meant before Search.
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
        "why": " ".join(str(data.get("why") or "").split())[:300],
        "notes": " ".join(str(data.get("notes") or "").split())[:500],
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


# Run-once controls: clicking one of these twice in a run means the planner lost its place, and could order or send twice.
_ONCE = re.compile(r"\b(finish|complete|submit|confirm|place|checkout)\b", re.I)


def once_labels(actions: list[dict], state: dict) -> list[str]:
    """Labels of the batch's clicks that must not happen twice in one run."""
    elements = {str(e.get("ref")): e for e in state.get("elements") or [] if isinstance(e, dict)}
    out = []
    for step in actions:
        if str(step.get("kind") or "").lower() not in {"click", "dblclick"}:
            continue
        label = str((elements.get(str(step.get("ref") or "")) or {}).get("name") or "").strip()
        if label and (_is_final_label(label) or _ONCE.search(label)):
            out.append(label)
    return out


def with_progress(last: str, memory: dict) -> str:
    """What the planner reads as "Last turn": everything this run has done, so it can't lose its place."""
    parts = []
    if memory.get("done"):
        parts.append("Done so far (do not redo):\n" + "\n".join(f"{n}. {row}" for n, row in enumerate(memory["done"][-25:], 1)))
    if memory.get("notes"):
        parts.append("Your notes so far: " + " | ".join(memory["notes"][-20:]))
    parts.append(last or "start")
    return "\n".join(parts)


def _is_final_label(label: str) -> bool:
    return bool(_FINAL.search(label) or _FINAL_HI.search(label))


def _on_payment_page(state: dict) -> bool:
    return bool(_PAYMENT_PAGE.search(str(state.get("text") or "")) or _PAYMENT_URL.search(str(state.get("url") or "")))


def final_click(actions: list[dict], state: dict) -> str:
    """Name of a final/irreversible control the batch would click, else "". On a payment step this also covers
    generic labels ("Proceed"), buttons with no label (icons), and pressing Enter."""
    elements = {str(e.get("ref")): e for e in state.get("elements") or [] if isinstance(e, dict)}
    paying = _on_payment_page(state)
    for step in actions:
        kind = str(step.get("kind") or "").lower()
        if kind not in {"click", "dblclick", "press"}:
            continue
        el = elements.get(str(step.get("ref") or ""))
        label = str((el or {}).get("name") or "").strip()
        if label and _is_final_label(label):
            return label
        if not paying:
            continue
        if kind == "press":
            if str(step.get("text") or step.get("key") or "Enter").strip().lower() in {"enter", "return"}:
                return "the Enter key on a payment page"
        elif label and _GENERIC_ADVANCE.fullmatch(label):
            return label
        elif el is not None and not label and str(el.get("role") or "") in {"button", "link"}:
            return "an unlabeled button on a payment page"
    return ""


def trace_final_step(steps: list[dict]) -> str:
    """Name of a final/irreversible control a saved run clicks, else "". A run saved when the final action was
    allowed must not repeat it later just because the goal matches."""
    for step in steps or []:
        if str(step.get("kind") or "").lower() == "goal":
            text = _PLACEHOLDER.sub(" ", str(step.get("text") or ""))
            if _is_final_label(text):
                hit = _FINAL.search(text) or _FINAL_HI.search(text)
                return hit.group(0) if hit else text[:60]
            continue
        if str(step.get("kind") or "").lower() not in {"click", "dblclick"}:
            continue
        target = step.get("target") if isinstance(step.get("target"), dict) else {}
        for key in ("name", "aria_label", "label"):
            label = str(target.get(key) or "")
            if label and _is_final_label(label):
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


# --- task size: long flows get the main model and more rounds ------------------------------------------------------
MAX_ROUNDS = 40
LONG_ROUNDS = 30
_STEP_LINE = re.compile(r"^\s*(?:[-*•]|\(?\d{1,2}[.)]|step\s*\d+)\s*\S", re.I | re.M)
_THEN = re.compile(r"\b(?:then|after that|next|and then|finally)\b|[;→]|->", re.I)


def is_long_goal(goal: str) -> bool:
    """A goal that spells out a long flow (several listed steps, many 'then's, or a lot of text): the small model with
    12 rounds runs out before it ends."""
    text = str(goal or "")
    return len(_STEP_LINE.findall(text)) >= 5 or len(_THEN.findall(text)) >= 5 or len(text.split()) > 120


def round_budget(goal: str, max_steps: int = 0) -> tuple[int, bool]:
    """(model rounds, start on the main model) for a browser_task. The caller's max_steps wins (up to MAX_ROUNDS)."""
    long = is_long_goal(goal)
    default = int(setting("browser_fast_max_steps", 12) or 12)
    if max_steps:
        return max(1, min(int(max_steps), MAX_ROUNDS)), long
    return (max(default, LONG_ROUNDS) if long else default), long


async def add_saved_login(user_id, host: str, vals: dict[str, str]) -> bool:
    """Add the saved sign-in for this site to a run's values (the caller's own values win). True if one was added."""
    if not host:
        return False
    from app.browser_runs import logins

    saved = await logins.values_for(user_id, host)
    for key, value in saved.items():
        vals.setdefault(key, value)
    return bool(saved)


def summarize_actions(actions: list[dict], state: dict, values: dict[str, str]) -> str:
    """A batch in words for the run history: 'fill "Email" with {{login_username}}, click "Sign in"'."""
    names = {str(e.get("ref")): str(e.get("name") or "") for e in state.get("elements") or [] if isinstance(e, dict)}
    parts = []
    for a in actions[:MAX_BATCH]:
        kind = str(a.get("kind") or "")
        ref = str(a.get("ref") or "")
        target = f' "{names[ref][:50]}"' if names.get(ref) else (f" {ref}" if ref else "")
        text = str(a.get("text") or a.get("value") or a.get("url") or "")
        placeholder = bool(_PLACEHOLDER.fullmatch(text.strip())) if text.strip() else False
        shown = text.strip() if placeholder else repr(scrub(text, values)[:50])
        if kind in {"fill", "type"}:
            parts.append(f"{kind}{target} with {shown}")
        elif kind in {"select", "pick"}:
            parts.append(f"choose {shown} in{target}")
        elif kind in {"navigate", "press", "find", "wait_for"}:
            parts.append(f"{kind} {scrub(text, values)[:80]}".strip())
        else:
            parts.append(f"{kind}{target}")
    return ", ".join(parts)[:300]


async def browser_task(ctx, goal: str, url: str = "", values: dict | None = None, max_steps: int = 0,
                       allow_final_action: bool = False, use_saved: bool = True, save: bool = True) -> dict:  # fmt: skip
    """browser_task, kept in the run history (docs/BROWSING.md "Run history"). A call made inside another run (healing
    a step, an AI step) adds its steps to that run."""
    from app.browser_runs import runlog

    user_id = getattr(getattr(ctx, "user", None), "id", None)
    if runlog.CURRENT.get() is not None or user_id is None or not setting("browser_run_log", True):
        return await _browser_task(ctx, goal, url, values, max_steps, allow_final_action, use_saved, save)
    run = runlog.RunLog(user_id, goal, host_of(url))
    token = runlog.CURRENT.set(run)
    try:
        out = await _browser_task(ctx, goal, url, values, max_steps, allow_final_action, use_saved, save)
        if bot_walled(out) and await desktop_available(ctx):
            # The site blocked the cloud browser. The user's own browser (the Lumio desktop app, online now) has their
            # normal IP and sign-ins, so try once more there.
            await _log("Switched to your computer's browser",
                       "The site showed a bot check to the cloud browser, so this runs in your Lumio desktop app instead.",
                       True, None, shot=False)
            out = await on_desktop(ctx, _browser_task(ctx, goal, url, values, max_steps, allow_final_action, use_saved, save))
            if isinstance(out, dict):
                out["ran_on"] = "desktop"
                out.setdefault("note", "The site blocked the cloud browser, so this ran in your Lumio desktop app's browser.")
    finally:
        runlog.CURRENT.reset(token)
    if isinstance(out, dict):
        if not run.host:
            run.host = host_of(str((out.get("page") or {}).get("url") or ""))
        await run.finish(out, str(out.pop("_trace_id", "") or ""))
        if run._saved:
            out["run_id"] = str(run.id)
    return out


BOT_WALL_NEED = "The page needs a person (bot check or hand-off)."


def page_shape(state: Any, ctx: Any = None) -> dict:
    """What a page result contained, for diagnosing blank pages: its fields and sizes and which browser it came from.
    Never the page's text, elements or full address (they can hold personal data)."""
    from app.desktop.client import client_kind

    st = state if isinstance(state, dict) else {}
    error = re.sub(r"\?\S*", "?…", str(st.get("error") or ""))[:200]  # no query strings: they can hold values
    return {
        "type": type(state).__name__,
        "keys": sorted(str(k) for k in st)[:40],
        "ok": st.get("ok"),
        "ran_on": st.get("ran_on"),
        "host": host_of(str(st.get("url") or "")),
        "has_url": bool(st.get("url")),
        "title_len": len(str(st.get("title") or "")),
        "text_len": len(str(st.get("text") or "")),
        "tree_len": len(str(st.get("tree") or "")),
        "elements": len(st.get("elements") or []) if isinstance(st.get("elements"), list) else None,
        "unread": st.get("unread"),
        "bot_wall": bool(st.get("bot_wall")),
        "has_screenshot": bool(st.get("screenshot") or st.get("screenshot_path") or st.get("screenshot_b64")),
        "error": error,
        "client": client_kind(),
        "switched_to_desktop": bool(getattr(ctx, "_browser_on_desktop", False)),
        "switch_reason": str(getattr(ctx, "_browser_switch_reason", "") or ""),
    }


def _log_blank(where: str, state: Any, ctx: Any) -> None:
    try:
        log.warning("browser.task blank page where=%s shape=%s", where, json.dumps(page_shape(state, ctx), default=str))
    except Exception:  # noqa: BLE001 - diagnostics must never break a run
        log.info("blank page shape not logged", exc_info=True)


def _on_desktop(ctx) -> bool:
    from app.agent.computer_harness import browser_local

    return browser_local(ctx)


def vision_possible(ctx) -> bool:
    """Browsing by sight is on, and this turn's browser is the cloud one (the desktop app's own screen needs its next
    version to report screen scale)."""
    from app.agent.computer_harness import browser_local

    return bool(setting("browser_vision", True)) and not browser_local(ctx)


async def by_sight(ctx, goal: str, vals: dict[str, str], allow_final: bool, log_steps: list) -> dict:
    """Hand the task to the screenshot loop (vision_browser.computer_task)."""
    from app.agent.vision_browser import computer_task

    await _log("Switched to working by sight", "The page's structure wasn't enough, so this continues from screenshots.",
               True, None, shot=False)
    out = await computer_task(ctx, goal, vals, allow_final_action=allow_final)
    if isinstance(out, dict):
        out.setdefault("log", log_steps)
        log.info("browser.task by sight: done=%s fallback=%s unsupported=%s error=%s via=%s", out.get("done"), out.get("fallback"),
                 out.get("unsupported"), out.get("error"), out.get("via"))
    return out


def bot_walled(out: Any) -> bool:
    return isinstance(out, dict) and not out.get("done") and out.get("need_user") == BOT_WALL_NEED


async def desktop_available(ctx) -> bool:
    """The chat runs in the cloud browser, and the user's desktop app is online and not set to "cloud only"."""
    from app.agent.computer_harness import browser_local, desktop_online

    return not browser_local(ctx) and await desktop_online(ctx)


async def on_desktop(ctx, work):
    """Run ``work`` with the browser tools on the user's desktop app, and keep them there for the rest of the turn."""
    from app.agent.computer_harness import switch_to_desktop

    switch_to_desktop(ctx, "the site blocked the cloud browser")
    return await work


async def _log(action: str, why: str = "", ok: bool = True, state: dict | None = None, error: str = "", shot: bool = True) -> None:
    from app.browser_runs import runlog

    run = runlog.CURRENT.get()
    if run is not None:
        await run.step(action, why, ok, state, error, shot)


async def _browser_task(ctx, goal: str, url: str = "", values: dict | None = None, max_steps: int = 0,
                        allow_final_action: bool = False, use_saved: bool = True, save: bool = True) -> dict:  # fmt: skip
    memory: dict = {"done": [], "notes": [], "once": set()}
    out = await _run_task(ctx, goal, url, values, max_steps, allow_final_action, use_saved, save, memory)
    if isinstance(out, dict) and memory["done"]:
        out["done_so_far"] = memory["done"][-25:]
        if memory["notes"]:
            out["notes"] = memory["notes"][-20:]
        if not out.get("done"):
            # Whoever carries on must not do these again: a repeated checkout is a second order.
            out["do_not_repeat"] = "The steps in done_so_far already happened. Continue from there; never start the task over."
    return out


async def _run_task(ctx, goal: str, url: str, values: dict | None, max_steps: int, allow_final_action: bool, use_saved: bool,
                    save: bool, memory: dict) -> dict:  # fmt: skip
    """See the module docstring. Never raises: problems come back as ``fallback: true`` with the page.
    ``save=False`` (healing a saved run) keeps the run unsaved and returns its steps as ``_recorded``."""
    vals = {str(k): str(v) for k, v in (values or {}).items() if v is not None}
    goal = str(goal or "").strip()
    started = time.monotonic()
    log_steps: list[dict] = []
    limit, long_goal = round_budget(goal, max_steps)
    user_id = getattr(getattr(ctx, "user", None), "id", None)
    try:
        state: dict = {}
        target_host = host_of(url)
        # 0. A sign-in the user saved for this site: code types it as {{login_username}} / {{login_password}}.
        if user_id is not None and setting("browser_saved_logins", True):
            if not target_host:
                state = await _snapshot(ctx)
                target_host = host_of(str(state.get("url") or ""))
            await add_saved_login(user_id, target_host, vals)
        # 1. A saved run of the same task: replay it with no model calls.
        if setting("browser_replay", True) and use_saved and user_id is not None:
            if not target_host:
                state = await _snapshot(ctx)
                target_host = host_of(str(state.get("url") or ""))
            rows = await STORE.candidates(user_id, host=target_host) if target_host else []
            trace = pick_trace(rows, goal=goal)
            if trace is not None and not allow_final_action and trace_final_step(trace.get("steps") or []):
                trace = None  # the model loop below stops at that control and asks
            if trace is not None:
                t0 = time.monotonic()
                await add_filled_variables(ctx, trace, vals, goal)
                rep = await replay(ctx, trace, vals, heal=setting("browser_heal", True), allow_final=allow_final_action)
                if rep.get("needs_approval"):
                    ask = rep["needs_approval"]
                    return _result(rep.get("state") or {}, vals, ok=True, done=False, via="replay", trace=trace.get("name"),
                                   needs_approval=ask, run_id_saved=str(trace.get("id") or ""), need_user=f"Waiting for your OK: {ask['text']}",
                                   hint=(f"Ask the user \"{ask['text']}\" (ask_user); only on a clear yes call browser_replay(run_id=\""
                                         f"{trace.get('id')}\", start_step={ask['step']}, same values)."), log=log_steps)  # fmt: skip
                await STORE.mark(trace["id"], bool(rep.get("ok")) and not rep.get("healed"))
                log_steps.append({"via": "replay", "ok": bool(rep.get("ok")), "steps": rep.get("completed"),
                                  "healed": len(rep.get("healed") or []), "ms": int((time.monotonic() - t0) * 1000)})  # fmt: skip
                await _log(f"Replayed the saved run \"{trace.get('name')}\" ({rep.get('completed') or 0} steps)",
                           "A saved run of this task exists, so its steps ran without the model.", bool(rep.get("ok")),
                           rep.get("state"), scrub(str(rep.get("error") or ""), vals))  # fmt: skip
                if rep.get("ok"):
                    proposed = await _propose_fix(trace, rep)
                    return _result(rep.get("state") or {}, vals, ok=True, done=True, via="replay", trace=trace.get("name"),
                                   result="Replayed the saved steps; check the page below.", log=log_steps, _trace_id=str(trace.get("id") or ""),
                                   took_ms=int((time.monotonic() - started) * 1000), **heal_fields(rep, proposed))  # fmt: skip
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
        state = await settle(ctx, state)
        if not start_url:
            start_url = str(state.get("url") or "")
        if page_empty(state) and not _blocked(state):
            _log_blank("start", state, ctx)
        if page_empty(state) and not _blocked(state) and vision_possible(ctx):
            # The page shows nothing the element tree can read (canvas, late rendering): work from the screen. If that
            # isn't possible here, carry on with the element-tree loop below as before.
            seen = await by_sight(ctx, goal, vals, allow_final_action, log_steps)
            if not seen.get("fallback"):
                return seen
        if _blocked(state):
            return _result(state, vals, ok=False, done=False, need_user=BOT_WALL_NEED, log=log_steps)
        model, strong = fast_model(), escalate_model()
        if long_goal:
            model = strong  # a long flow needs the planner that keeps the whole plan in mind
        escalated = model == strong
        failures, last, redirects, blank_rounds = 0, "", 0, 0
        for i in range(limit):
            t0 = time.monotonic()
            try:
                plan = await plan_step(model, goal, state, vals, with_progress(last, memory), allow_final_action)
            except Exception as exc:  # noqa: BLE001 - model error: escalate or fall back
                log.info("browser.task plan failed model=%s: %s", model, type(exc).__name__)
                plan = None
            plan_ms = int((time.monotonic() - t0) * 1000)
            if plan and plan.get("notes") and scrub(plan["notes"], vals) not in memory["notes"]:
                memory["notes"].append(scrub(plan["notes"], vals))
            if plan is None or (not plan["actions"] and not plan["done"] and not plan["need_user"]):
                failures += 1
                last = "Your last reply was not a usable JSON plan. Reply with the JSON object only."
                log_steps.append({"step": i + 1, "model": model, "plan_ms": plan_ms, "ok": False, "why": "no plan"})
            elif plan["need_user"] and need_user_kind(plan["need_user"]) != "real":
                # Not a step only a person can take: answer it here and keep going. The first two are free; after
                # that they count toward escalation and the fallback, so a planner that keeps asking can't loop.
                redirects += 1
                if redirects > 2:
                    failures += 1
                if need_user_kind(plan["need_user"]) == "site_choice":
                    last = ("The user did not name a site, so choose a well-known one yourself (for flights, for example, "
                            "the airline's own site, MakeMyTrip or Google Flights) and continue. Do not ask the user.")
                else:
                    state = await settle(ctx, await _snapshot(ctx))
                    last = ("Do not ask the user for page details, elements or how to use the page: you have the page "
                            "below (looked at again just now). Use the elements listed; for dates use pick_date; if the "
                            "list is empty, wait_for a visible word or navigate to the page again.")
                log_steps.append({"step": i + 1, "model": model, "plan_ms": plan_ms, "ok": False, "why": "asked the user for something it can do"})
            elif plan["need_user"]:
                await _log("Stopped: needs you", plan["need_user"], False, state)
                return _result(state, vals, ok=False, done=False, need_user=plan["need_user"], log=log_steps,
                               took_ms=int((time.monotonic() - started) * 1000))  # fmt: skip
            elif plan["done"] and not plan["actions"]:
                if not save:
                    return _result(state, vals, ok=True, done=True, via="model", result=scrub(plan["result"], vals),
                                   log=log_steps, _recorded=recorded)  # fmt: skip
                trace_id = await _save(user_id, goal, start_url, recorded, vals)
                log_steps.append({"step": i + 1, "model": model, "plan_ms": plan_ms, "done": True})
                return _result(state, vals, ok=True, done=True, via="model", result=scrub(plan["result"], vals),
                               saved_trace=bool(trace_id), _trace_id=trace_id or "", log=log_steps, took_ms=int((time.monotonic() - started) * 1000))  # fmt: skip
            else:
                actions = plan["actions"]
                blocked = "" if allow_final_action else final_click(actions, state)
                if blocked:
                    return _result(state, vals, ok=True, done=False, stopped_before=blocked,
                                   result=f"Stopped before '{blocked}' (a final action). If the user asked for it in chat, call again with allow_final_action; otherwise ask them first.",
                                   log=log_steps, took_ms=int((time.monotonic() - started) * 1000))  # fmt: skip
                once = once_labels(actions, state)
                again = next((label for label in once if label.casefold() in memory["once"]), "")
                if again:
                    log.warning("browser.task refused to click a run-once control twice")
                    await _log(f"Stopped before clicking '{again}' again", "It was already clicked in this run.", False, state, shot=False)
                    return _result(state, vals, ok=True, done=False, stopped_repeat=again,
                                   result=f"Stopped: '{again}' was already clicked once in this run, so it was not clicked again. Report what was done.",
                                   log=log_steps, took_ms=int((time.monotonic() - started) * 1000))  # fmt: skip
                finds = [a for a in actions if str(a.get("kind") or "").lower() == "find"]
                if finds:
                    words = " ".join(str(a.get("text") or a.get("value") or "") for a in finds).strip()
                    state = await _snapshot(ctx, words)
                    await _log(f"find {words}", plan.get("why", ""), True, state, shot=False)
                    last = f"Re-listed the page with elements matching {words!r} first. Pick refs from the new list."
                    log_steps.append({"step": i + 1, "model": model, "plan_ms": plan_ms, "actions": ["find"], "ok": True})
                    continue
                t1 = time.monotonic()
                before = state
                state, ok, err, kept = await _run_batch(ctx, actions, vals)
                state = await settle(ctx, state)
                blank_rounds = blank_rounds + 1 if page_empty(state) else 0
                if blank_rounds:
                    _log_blank(f"after_step_{i + 1}", state, ctx)
                if blank_rounds >= 2:
                    # The page still reads as blank after waiting, twice in a row. Opening it again won't help (seen
                    # live: MakeMyTrip re-opened five times, ~17 s each, while it was showing fine on screen).
                    reason = str(state.get("error") or "")[:300]
                    log.warning("browser.task: page reads blank twice (%s) url=%s error=%s", "desktop" if _on_desktop(ctx) else "cloud",
                                str(state.get("url") or "")[:120], reason or "-")
                    await _log("Page can't be read", reason or "The page showed nothing readable twice after loading.", False, state)
                    if vision_possible(ctx):
                        seen = await by_sight(ctx, goal, vals, allow_final_action, log_steps)
                        if not seen.get("fallback"):
                            return seen
                    return _result(state, vals, ok=False, done=False, fallback=True, blank_page=True,
                                   note=("The page loaded but couldn't be read" + (f" ({reason})" if reason else "") +
                                         ". Don't open it again; try browser_snapshot once, then tell the user plainly."),
                                   log=log_steps, took_ms=int((time.monotonic() - started) * 1000))  # fmt: skip
                await _log(summarize_actions(actions, before, vals), plan.get("why", ""), bool(ok), state,
                           "" if ok or isinstance(err, list) else scrub(str(err or ""), vals))  # fmt: skip
                recorded.extend(kept)
                ran = int(state.get("completed") or 0) if not ok else len(actions)
                if ok or ran:
                    path = re.sub(r"[?#].*$", "", str(state.get("url") or ""))[-80:]
                    memory["done"].append(f"{summarize_actions(actions[:ran], before, vals)}"[:240] + (f" (now at {path})" if path else ""))
                    memory["once"].update(label.casefold() for label in once_labels(actions[:ran], before))
                act_ms = int((time.monotonic() - t1) * 1000)
                log_steps.append({"step": i + 1, "model": model, "plan_ms": plan_ms, "act_ms": act_ms, "actions": [a.get("kind") for a in actions], "ok": ok})
                log.info(
                    "browser.task step=%d model=%s plan_ms=%d act_ms=%d actions=%s ok=%s",
                    i + 1, model, plan_ms, act_ms, ",".join(str(a.get("kind")) for a in actions), ok,
                )  # fmt: skip
                if _blocked(state):
                    return _result(state, vals, ok=False, done=False, need_user=BOT_WALL_NEED, log=log_steps)
                if isinstance(err, list):
                    return _result(state, vals, ok=False, done=False, need_user="Missing values: " + ", ".join(err), log=log_steps)
                warned = [str(w) for w in (state.get("warnings") or [])][:3] if ok else []
                if ok and not (warned and not plan["done"]):
                    last = f"Ran {len(actions)} action(s): {', '.join(str(a.get('kind')) for a in actions)}. All worked."
                    if plan["done"]:
                        if not save:
                            return _result(state, vals, ok=True, done=True, via="model", result=scrub(plan["result"], vals),
                                           log=log_steps, _recorded=recorded)  # fmt: skip
                        trace_id = await _save(user_id, goal, start_url, recorded, vals)
                        return _result(state, vals, ok=True, done=True, via="model", result=scrub(plan["result"], vals),
                                       saved_trace=bool(trace_id), _trace_id=trace_id or "", log=log_steps, took_ms=int((time.monotonic() - started) * 1000))  # fmt: skip
                    continue
                failures += 1
                if ok:
                    # Every step ran, but a click changed nothing: count it like a failure so a repeat escalates.
                    last = f"Ran {len(actions)} action(s), but: " + " ".join(warned)
                else:
                    last = scrub(f"A step failed: {err}. Use the refs in the fresh page below.", vals)
            if failures >= 2:
                if not escalated:
                    model, escalated, failures = strong, True, 0
                    log.info("browser.task escalating to model=%s", model)
                    continue
                break
        if vision_possible(ctx):
            # The element tree didn't get there: try again by looking at the screen, like a person would.
            seen = await by_sight(ctx, goal, vals, allow_final_action, log_steps)
            if not seen.get("fallback"):
                return seen
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


async def replay_named(ctx, name: str = "", url: str = "", skill_id: str = "", values: dict | None = None,
                       allow_final_action: bool = False, run_id: str = "", start_step: int = 1) -> dict:  # fmt: skip
    """browser_replay: run a saved flow (a finished browser_task, or a taught skill's DOM recording) by name, site
    or skill id, without model calls. On a mismatch it stops and returns the page so the caller continues."""
    vals = {str(k): str(v) for k, v in (values or {}).items() if v is not None}
    user_id = getattr(getattr(ctx, "user", None), "id", None)
    if user_id is None:
        return {"ok": False, "error": "no user for saved runs"}
    host = host_of(url)
    if run_id:
        rows = await STORE.candidates(user_id, trace_id=run_id)
        trace = rows[0] if rows else None
    else:
        rows = await STORE.candidates(user_id, host=host, skill_id=skill_id)
        trace = pick_trace(rows, name=name, goal=name) if name else (rows[0] if rows else None)
    if trace is None:
        return {
            "ok": False,
            "error": "no saved run matches",
            "saved": [{"name": r.get("name"), "host": r.get("host"), "params": r.get("params")} for r in rows[:10]],
            "hint": "Use browser_task (it saves the run when it finishes) or browser_act.",
        }
    blocked = "" if allow_final_action else trace_final_step(trace.get("steps") or [])
    if blocked:
        return {"ok": False, "trace": trace.get("name"), "stopped_before": blocked,
                "hint": f"This saved run clicks '{blocked}' (a final action), so it was not run. If the user asked for it in chat, call again with allow_final_action; otherwise ask them first."}  # fmt: skip
    if setting("browser_saved_logins", True):
        await add_saved_login(user_id, str(trace.get("host") or host_of(str(trace.get("start_url") or ""))), vals)
    filled = await add_filled_variables(ctx, trace, vals, name)
    started = time.monotonic()
    rep = await replay(ctx, trace, vals, heal=setting("browser_heal", True), allow_final=allow_final_action, start_step=start_step)
    if rep.get("missing"):
        return {"ok": False, "missing_values": rep["missing"], "params": trace.get("params"), "hint": "Pass them in values."}
    if not rep.get("needs_approval"):
        await STORE.mark(trace["id"], bool(rep.get("ok")) and not rep.get("healed"))
    proposed = await _propose_fix(trace, rep)
    fields: dict[str, Any] = {"ok": bool(rep.get("ok")), "via": "replay", "trace": trace.get("name"), "steps": rep.get("completed"),
                              "took_ms": int((time.monotonic() - started) * 1000), **heal_fields(rep, proposed)}  # fmt: skip
    if filled:
        fields["variables_filled"] = filled
    if rep.get("needs_approval"):
        ask = rep["needs_approval"]
        fields.update(needs_approval=ask, run_id=str(trace.get("id") or ""),
                      hint=(f"The saved run pauses at step {ask['step']} for the user's OK: \"{ask['text']}\". Ask the user that "
                            f"(ask_user), and only on a clear yes call browser_replay(run_id=\"{trace.get('id')}\", "
                            f"start_step={ask['step']}, same values) to continue; on no, stop and say so."))  # fmt: skip
    elif not rep.get("ok"):
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
    if setting("browser_saved_logins", True) and setting("browser_fast_loop", True):
        bits.append(
            "Sign-ins the user saved in the app (browser_logins lists the sites) are filled in by code: in browser_task write "
            "{{login_username}} and {{login_password}} for the fields, never the values. Never ask the user to type a password "
            "in chat; if a site has no saved sign-in, ask them to add it on the Saved runs page or sign in themselves (browser_handoff). "
        )
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
