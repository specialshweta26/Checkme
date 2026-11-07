"""Cards in Chief's window for mail that really needs the user: a reply or response from a key contact, something
urgent, or something about a promise the user made. Built from the inbox triage that already ran (no extra model call)
and kept out of the chat history, so they never cost tokens or crowd the conversation.

A sender whose cards the user keeps dismissing (3 in 30 days, none answered from a card) stops getting key-contact
cards; urgent and promise cards still come through. Only the signed-in user's mail is read.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import InboxItem

NEEDS_USER = ("reply", "respond")
MUTE_AFTER = 3
MUTE_WINDOW = timedelta(days=30)
MAX_CARDS = 10
LABELS = {"promise": "About something you promised", "urgent": "Urgent", "key_contact": "Key contact"}


def _truthy(value) -> bool:
    return value is True or str(value).strip().lower() in ("true", "yes", "1")


def alert_reason(item, data: dict, ctx=None) -> str:
    """Why this email deserves a card, or "" if it doesn't. Strongest reason first."""
    if (item.classification or "") not in NEEDS_USER or (ctx is not None and getattr(ctx, "from_self", False)):
        return ""
    has_promise = ctx is not None and any("Open:" in line for line in (getattr(ctx, "people", None) or []))
    if has_promise and _truthy(data.get("about_promise")):
        return "promise"
    if _truthy(data.get("urgent")):
        return "urgent"
    cc_only = (getattr(item, "recipient_role", "") or "to") in ("cc", "bcc")
    if ctx is not None and getattr(ctx, "key_person", False) and not cc_only:
        return "key_contact"
    return ""


def mark_alert(item, data: dict, ctx=None) -> None:
    """Set the card on a freshly triaged item. A card the user already dismissed or answered stays that way."""
    reason = alert_reason(item, data, ctx)
    item.alert_reason = reason
    if reason and not getattr(item, "alert_state", ""):
        item.alert_state = "new"
        item.alerted_at = datetime.now(timezone.utc)
    elif not reason and getattr(item, "alert_state", "") == "new":
        item.alert_state = ""  # a redraft found it no longer needs the user


def _sender_email(sender: str) -> str:
    from app.memory_engine.entities import clean_email

    return clean_email(sender or "")


async def muted_senders(db: AsyncSession, user_id) -> set[str]:
    """Senders whose cards the user dismissed MUTE_AFTER times in MUTE_WINDOW without ever answering from one."""
    since = datetime.now(timezone.utc) - MUTE_WINDOW
    rows = (await db.execute(
        select(InboxItem.sender, InboxItem.alert_state).where(
            InboxItem.user_id == user_id, InboxItem.alert_state.in_(("dismissed", "sent")), InboxItem.alerted_at >= since)
    )).all()
    dismissed: dict[str, int] = {}
    answered: set[str] = set()
    for sender, state in rows:
        email = _sender_email(sender)
        if state == "sent":
            answered.add(email)
        else:
            dismissed[email] = dismissed.get(email, 0) + 1
    return {e for e, n in dismissed.items() if n >= MUTE_AFTER and e not in answered}


async def open_alerts(db: AsyncSession, user_id) -> list[InboxItem]:
    rows = (await db.execute(
        select(InboxItem).where(InboxItem.user_id == user_id, InboxItem.status == "pending", InboxItem.alert_state == "new",
                                InboxItem.alert_reason != "")
        .order_by(func.coalesce(InboxItem.alerted_at, InboxItem.created_at).desc()).limit(MAX_CARDS * 3)
    )).scalars().all()
    muted = await muted_senders(db, user_id)
    shown = [r for r in rows if r.alert_reason in ("urgent", "promise") or _sender_email(r.sender) not in muted]
    order = {"promise": 0, "urgent": 1, "key_contact": 2}
    shown.sort(key=lambda r: order.get(r.alert_reason, 3))
    return shown[:MAX_CARDS]
