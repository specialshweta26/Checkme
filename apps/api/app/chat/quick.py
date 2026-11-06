"""Turns that need no agent run (docs/LLD.md "Instant turns").

- Small talk ("hi", "thanks", "bye") is answered at once, without connectors, memory, skills or a model call, and
  never stops a task that is running. If the last task in the chat was stopped, the reply offers to continue it; if
  one is running, it says so.
- "continue" / "go on" / "resume" right after a stopped turn picks that task up, like the Continue button.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.agent.fast_path import is_small_talk

RUNNING_FRESH = timedelta(minutes=20)
_CONTINUE = re.compile(
    r"\s*(?:ok(?:ay)?[,\s]+|yes[,\s]+|please\s+|pls\s+)?(continue|go on|go ahead|resume|carry on|keep going|proceed|finish it|continue please|"
    r"continue the task|continue where you (?:left off|stopped))(?:\s+please|\s+pls)?[\s!.]*",
    re.I,
)
_THANKS = re.compile(r"\s*(thanks|thank you|thx)\b", re.I)
_BYE = re.compile(r"\s*(bye|good night)\b", re.I)
_OK = re.compile(r"\s*(ok(?:ay)?|cool|great|nice)\b", re.I)
_HOW = re.compile(r"\s*(how are you|how's it going|what's up|sup)\b", re.I)


def is_continue(text: str) -> bool:
    return bool(_CONTINUE.fullmatch(text or ""))


def short_task(text: str, limit: int = 90) -> str:
    one = " ".join(str(text or "").split())
    return one if len(one) <= limit else one[: limit - 1].rstrip() + "…"


async def last_turn_state(db, sess) -> dict:
    """{"stopped": request_id, "task": text} for a chat whose last assistant reply is a stop marker, or
    {"running": True, "task": text} when a turn is still working on it, else {}."""
    from app.models import ChatMessage

    rows = (
        await db.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == sess.id, ChatMessage.role.in_(("user", "assistant")))
            .order_by(ChatMessage.id.desc())
            .limit(6)
        )
    ).scalars().all()
    last = next((m for m in rows if m.role == "assistant"), None)
    if last is None:
        return {}
    meta = last.meta if isinstance(last.meta, dict) else {}
    asked = next((m for m in rows if m.role == "user" and m.request_id and m.request_id == last.request_id), None)
    task = short_task(asked.content if asked is not None else "")
    if meta.get("stopped") and meta.get("can_continue") and last.request_id:
        return {"stopped": last.request_id, "task": task}
    if meta.get("in_progress"):
        when = last.created_at
        if when is not None and when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when is None or datetime.now(timezone.utc) - when < RUNNING_FRESH:
            return {"running": True, "task": task}
    return {}


def _hello(now: datetime | None = None) -> str:
    hour = (now or datetime.now()).hour
    if 5 <= hour < 12:
        return "Good morning"
    if 12 <= hour < 17:
        return "Good afternoon"
    if 17 <= hour < 23:
        return "Good evening"
    return "Hi"


def reply(text: str, agent_name: str = "", state: dict | None = None, now: datetime | None = None) -> str:
    """The instant answer to small talk, aware of a stopped or running task in this chat."""
    st = state or {}
    task = st.get("task") or ""
    if _THANKS.match(text or ""):
        head = "You're welcome!"
    elif _BYE.match(text or ""):
        head = "Bye for now!"
    elif _HOW.match(text or ""):
        head = "All good, thanks for asking."
    elif _OK.match(text or ""):
        head = "Great."
    else:
        head = f"{_hello(now)}! " + (f"{agent_name} here." if agent_name else "")
        head = head.strip()
    if st.get("running"):
        return f"{head} I'm still working on “{task}”. I'll post here when it's done, or tap Stop if you'd like me to drop it."
    if st.get("stopped"):
        return f"{head} I stopped “{task}” earlier. Say **continue** to pick it up where it stopped, or tell me what you need."
    if _BYE.match(text or "") or _THANKS.match(text or ""):
        return head
    return f"{head} What can I do for you?"


def instant(text: str, *, uploads: bool, confirms: bool, group: bool, resuming: bool) -> bool:
    """Small talk that can be answered without an agent run."""
    return is_small_talk(text) and not (uploads or confirms or group or resuming)
