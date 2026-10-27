from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.runtime import RuntimeConnector
from app.llm import chat_complete
from app.models import Connector, InboxItem, User
from connector_core.text import clip

log = logging.getLogger(__name__)

CLASSIFICATIONS = ("reply", "respond", "discard")
ACTIVE = ("pending",)
HIDDEN = ("sent", "discarded")
KEEP_PENDING = 40
MAX_CLASSIFY = 6
_INGEST_LOCKS: dict[uuid.UUID, asyncio.Lock] = {}
BATCH_FOLDERS = {"empty_spam": "spam", "empty_drafts": "drafts", "empty_trash": "trash"}
_MONTHS = {name: i for i, name in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
_DATE_NAMED = re.compile(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4}),\s+(\d{1,2}):(\d{2})\s*(AM|PM)", re.I)
_DATE_NUMERIC = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})(?:[,\s]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?")


def received_sort_key(received_at: str | None, fallback: datetime | None = None) -> datetime:
    text = (received_at or "").strip()
    named = _DATE_NAMED.search(text)
    if named:
        day, mon, year, hour, minute, ap = named.groups()
        month = _MONTHS.get(mon.title())
        if month:
            h = int(hour) % 12
            if ap.upper() == "PM":
                h += 12
            return datetime(int(year), month, int(day), h, int(minute), tzinfo=timezone.utc)
    numeric = _DATE_NUMERIC.search(text)
    if numeric:
        day, month, year, hour, minute, second = numeric.groups()
        return datetime(
            int(year),
            int(month),
            int(day),
            int(hour or 0),
            int(minute or 0),
            int(second or 0),
            tzinfo=timezone.utc,
        )
    if fallback is not None:
        return fallback if fallback.tzinfo else fallback.replace(tzinfo=timezone.utc)
    return datetime.min.replace(tzinfo=timezone.utc)


def newest_first(items: list) -> list:
    return sorted(items, key=lambda i: received_sort_key(getattr(i, "received_at", None), getattr(i, "created_at", None)), reverse=True)


def normalize_class(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if raw in CLASSIFICATIONS:
        return raw
    if raw in {"needs reply", "need reply", "answer", "write back"}:
        return "reply"
    if raw in {"acknowledge", "follow up", "follow-up", "action", "confirm"}:
        return "respond"
    if raw in {"ignore", "archive", "fyi", "normal", "no reply", "noreply"}:
        return "discard"
    return ""


def parse_json_object(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}


def apply_triage(item: InboxItem, data: dict[str, Any]) -> None:
    item.classification = normalize_class(data.get("classification")) or item.classification or "respond"
    item.summary = clip(str(data.get("summary") or ""), 2000)
    points = data.get("key_points") or data.get("bullets") or []
    if isinstance(points, str):
        points = [p.strip() for p in points.split("\n") if p.strip()]
    item.key_points = [clip(str(p), 240) for p in points[:8] if str(p).strip()]
    item.draft = clip(str(data.get("draft") or ""), 4000)
    if item.classification == "discard" and not item.draft:
        item.draft = "No reply is needed to this message."


def serialize(item: InboxItem, connector_name: str = "", connector_slug: str = "") -> dict[str, Any]:
    return {
        "id": str(item.id),
        "message_id": item.message_id,
        "thread_id": item.thread_id,
        "subject": item.subject,
        "from": item.sender,
        "to": getattr(item, "to_header", "") or "",
        "cc": getattr(item, "cc_header", "") or "",
        "recipient_role": getattr(item, "recipient_role", "") or "to",
        "date": item.received_at,
        "snippet": item.snippet,
        "classification": item.classification or "",
        "summary": item.summary,
        "key_points": item.key_points or [],
        "draft": item.draft,
        "status": item.status,
        "unread": item.unread,
        "seen": item.seen,
        "connector": connector_name,
        "connector_slug": connector_slug,
        "sort_at": received_sort_key(item.received_at, getattr(item, "created_at", None)).isoformat(),
    }


async def mail_connectors(db: AsyncSession, user: User) -> list[RuntimeConnector]:
    rows = (
        await db.execute(
            select(Connector)
            .where(
                Connector.user_id == user.id,
                Connector.enabled.is_(True),
                Connector.provider.in_(("gmail", "outlook")),
            )
            .order_by(Connector.created_at.asc())
        )
    ).scalars().all()
    return [RuntimeConnector(row) for row in rows]


async def connector_for(db: AsyncSession, user: User, connector_id) -> RuntimeConnector | None:
    row = await db.get(Connector, connector_id)
    if row is None or row.user_id != user.id or not row.enabled:
        return None
    return RuntimeConnector(row)


def _alert(item: InboxItem) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "subject": item.subject or "(no subject)",
        "from": item.sender,
        "date": item.received_at,
        "classification": item.classification or "",
        "recipient_role": getattr(item, "recipient_role", "") or "to",
        "unread": item.unread,
        "seen": item.seen,
    }


async def _pending_items(db: AsyncSession, user: User) -> list[InboxItem]:
    return newest_first(
        (
            await db.execute(select(InboxItem).where(InboxItem.user_id == user.id, InboxItem.status == "pending"))
        ).scalars().all()
    )


def split_kept(items: list, limit: int = KEEP_PENDING) -> tuple[list, list]:
    ranked = newest_first(items)
    return ranked[:limit], ranked[limit:]


async def _trim_pending(db: AsyncSession, user: User) -> list[InboxItem]:
    """Keep only the newest KEEP_PENDING cards. Older pending mail leaves the inbox view."""
    keep, drop = split_kept(await _pending_items(db, user))
    for item in drop:
        item.status = "discarded"
        item.unread = False
        item.seen = True
    if drop:
        await db.commit()
    return keep


async def _mark_unseen(db: AsyncSession, user: User) -> list[InboxItem]:
    rows = (
        await db.execute(select(InboxItem).where(InboxItem.user_id == user.id, InboxItem.seen.is_(False)))
    ).scalars().all()
    for row in rows:
        row.seen = True
    if rows:
        await db.commit()
    return list(rows)


def _badge_payload(items: list[InboxItem], *, arrived: int = 0, alerts: list[InboxItem] | None = None) -> dict[str, Any]:
    unseen = [item for item in items if not item.seen]
    shown = alerts if alerts is not None else unseen
    return {
        "count": len(unseen),
        "pending": len(items),
        "arrived": arrived,
        "items": [_alert(item) for item in shown[:15]],
    }


async def refresh_mailboxes(db: AsyncSession, user: User, *, unread_only: bool = True, classify: bool = True) -> int:
    runtimes = await mail_connectors(db, user)
    if not runtimes:
        return 0
    return await _ingest(db, user, runtimes, unread_only=unread_only, classify=classify)


def _connector_labels(runtimes: list[RuntimeConnector]) -> tuple[dict, dict]:
    names = {r.id: r.name for r in runtimes}
    slugs = {r.id: r.slug for r in runtimes}
    return names, slugs


async def _cards_payload(db: AsyncSession, user: User, items: list[InboxItem], **badge_kw) -> dict[str, Any]:
    runtimes = await mail_connectors(db, user)
    names, slugs = _connector_labels(runtimes)
    payload = _badge_payload(items, **badge_kw)
    payload["inbox"] = [serialize(i, names.get(i.connector_id, ""), slugs.get(i.connector_id, "")) for i in items]
    payload["connected"] = bool(runtimes)
    return payload


async def badge(db: AsyncSession, user: User) -> dict[str, Any]:
    return await _cards_payload(db, user, await _trim_pending(db, user))


async def mark_alerts_seen(db: AsyncSession, user: User) -> dict[str, Any]:
    snapshot = newest_first(await _mark_unseen(db, user))
    items = await _trim_pending(db, user)
    return await _cards_payload(db, user, items, alerts=snapshot)


async def list_inbox(db: AsyncSession, user: User, *, live: bool = False, mark_seen: bool = False, classify: bool = False) -> dict[str, Any]:
    runtimes = await mail_connectors(db, user)
    if not runtimes:
        return {"items": [], "count": 0, "pending": 0, "connected": False}
    if live:
        await _ingest(db, user, runtimes, unread_only=False, classify=classify)
    if mark_seen:
        await _mark_unseen(db, user)
    names, slugs = _connector_labels(runtimes)
    items = await _trim_pending(db, user)
    unseen = sum(1 for i in items if not i.seen)
    return {
        "items": [serialize(i, names.get(i.connector_id, ""), slugs.get(i.connector_id, "")) for i in items],
        "count": unseen,
        "pending": len(items),
        "connected": True,
    }


async def get_item(db: AsyncSession, user: User, item_id) -> InboxItem:
    item = await db.get(InboxItem, item_id)
    if item is None or item.user_id != user.id:
        raise LookupError("Inbox item not found")
    return item


async def save_draft(db: AsyncSession, user: User, item_id, draft: str) -> dict[str, Any]:
    item = await get_item(db, user, item_id)
    item.draft = clip(draft or "", 4000)
    await db.commit()
    return serialize(item)


async def rephrase(db: AsyncSession, user: User, item_id, *, tone: str = "", instruction: str = "") -> dict[str, Any]:
    item = await get_item(db, user, item_id)
    voice = await _voice(db, user)
    prompt = (
        "Rewrite this email draft. Keep every fact, name, date, and commitment. "
        "Return JSON {\"draft\":\"...\"} only.\n"
        f"Tone: {(tone or 'professional').strip()[:40]}\n"
        f"Extra instruction: {(instruction or 'none').strip()[:240]}\n"
        f"User writing voice: {clip(voice or 'plain, concise, professional', 800)}\n"
        f"Original classification: {item.classification}\n"
        f"Thread summary: {clip(item.summary, 1200)}\n"
        f"Current draft: {clip(item.draft, 3000)}\n"
    )
    data = parse_json_object(await _llm(prompt, 700, json_mode=True))
    next_draft = str(data.get("draft") or "").strip()
    if next_draft:
        item.draft = clip(next_draft, 4000)
        await db.commit()
    return serialize(item)


async def classify_pending(db: AsyncSession, user: User) -> int:
    runtimes = await mail_connectors(db, user)
    if not runtimes:
        return 0
    items = (
        (
            await db.execute(
                select(InboxItem).where(
                    InboxItem.user_id == user.id,
                    InboxItem.status == "pending",
                    InboxItem.classification == "",
                ).limit(MAX_CLASSIFY)
            )
        )
        .scalars()
        .all()
    )
    if not items:
        return 0
    await _classify_many(db, user, runtimes, list(items))
    return len(items)


def _address_list(values: list[str] | str | None) -> list[str]:
    if isinstance(values, str):
        parts = re.split(r"[\s,;]+", values)
    else:
        parts = list(values or [])
    out: list[str] = []
    for raw in parts:
        addr = (raw or "").strip().strip("<>")
        if "@" in addr and addr not in out:
            out.append(addr)
    return out


async def send_reply(
    db: AsyncSession,
    user: User,
    item_id,
    draft: str | None = None,
    attachment_keys: list[str] | None = None,
    cc: list[str] | str | None = None,
    bcc: list[str] | str | None = None,
    reply_all: bool = False,
) -> dict[str, Any]:
    item = await get_item(db, user, item_id)
    body = clip((draft if draft is not None else item.draft) or "", 4000).strip()
    if not body:
        raise ValueError("Draft is empty")
    runtime = await connector_for(db, user, item.connector_id)
    if runtime is None:
        raise LookupError("Mail connector is not available")
    item.draft = body
    from app.agent.invoke import _load_mail_files

    args = _load_mail_files(
        {
            "message_id": item.message_id,
            "body": body,
            "attachment_keys": attachment_keys or [],
            "reply_all": bool(reply_all),
            "cc": _address_list(cc),
            "bcc": _address_list(bcc),
        }
    )
    result = await runtime.call("reply", args)
    if isinstance(result, dict) and result.get("error"):
        raise RuntimeError(str(result.get("error")))
    try:
        await runtime.call("mark_read", {"message_ids": [item.message_id], "read": True})
    except Exception:  # noqa: BLE001
        log.warning("mark_read after send failed", exc_info=True)
    item.status = "sent"
    item.unread = False
    item.seen = True
    await db.commit()
    return {"ok": True, "item": serialize(item), "result": result}


async def delete_item(db: AsyncSession, user: User, item_id) -> dict[str, Any]:
    item = await get_item(db, user, item_id)
    runtime = await connector_for(db, user, item.connector_id)
    if runtime is None:
        raise LookupError("Mail connector is not available")
    result = await runtime.call("bulk_delete", {"message_ids": [item.message_id], "permanent": False, "dry_run": False})
    item.status = "discarded"
    item.unread = False
    item.seen = True
    await db.commit()
    return {"ok": True, "item": serialize(item), "result": result}


async def batch_mailbox(
    db: AsyncSession,
    user: User,
    action: str,
    *,
    confirm: bool = False,
    item_ids: list[str] | None = None,
) -> dict[str, Any]:
    key = (action or "").strip().lower()
    if key in BATCH_FOLDERS:
        return await _empty_all(db, user, BATCH_FOLDERS[key], confirm)
    if key in {"clear_discard", "delete_items"}:
        return await _delete_cards(db, user, key, confirm, item_ids or [])
    raise ValueError("Unknown batch action. Use empty_spam, empty_drafts, empty_trash, clear_discard, or delete_items.")


async def _empty_all(db: AsyncSession, user: User, folder: str, confirm: bool) -> dict[str, Any]:
    runtimes = await mail_connectors(db, user)
    if not runtimes:
        raise ValueError("Connect Gmail or Outlook first")
    connectors: list[dict[str, Any]] = []
    samples: list[Any] = []
    ids: list[str] = []
    total = 0
    for runtime in runtimes:
        try:
            result = await runtime.call("empty_folder", {"folder": folder, "dry_run": not confirm})
        except Exception as exc:  # noqa: BLE001
            log.warning("empty_folder failed for %s: %s", runtime.slug, exc)
            connectors.append({"connector": runtime.name, "folder": folder, "count": 0, "deleted": 0, "failed": 1, "error": str(exc)[:200]})
            continue
        if not isinstance(result, dict):
            result = {"count": 0}
        count = int(result.get("count") or result.get("deleted") or 0)
        total += count
        connectors.append(
            {
                "connector": runtime.name,
                "folder": result.get("folder") or folder,
                "count": count,
                "deleted": result.get("deleted") or 0,
                "failed": result.get("failed") or 0,
            }
        )
        samples.extend(result.get("sample") or [])
        ids.extend(str(x) for x in (result.get("message_ids") or result.get("draft_ids") or []) if x)
    if confirm and ids:
        rows = (
            await db.execute(select(InboxItem).where(InboxItem.user_id == user.id, InboxItem.message_id.in_(ids)))
        ).scalars().all()
        for row in rows:
            row.status = "discarded"
            row.unread = False
            row.seen = True
        await db.commit()
    return {
        "action": f"empty_{folder}",
        "folder": folder,
        "count": total,
        "deleted": total if confirm else 0,
        "confirm": confirm,
        "connectors": connectors,
        "sample": samples[:8],
    }


async def _delete_cards(
    db: AsyncSession,
    user: User,
    action: str,
    confirm: bool,
    item_ids: list[str],
) -> dict[str, Any]:
    query = select(InboxItem).where(InboxItem.user_id == user.id, InboxItem.status == "pending")
    if action == "clear_discard":
        query = query.where(InboxItem.classification == "discard")
    else:
        parsed = []
        for raw in item_ids:
            try:
                parsed.append(uuid.UUID(str(raw)))
            except ValueError:
                continue
        if not parsed:
            raise ValueError("No messages selected")
        query = query.where(InboxItem.id.in_(parsed))
    rows = (await db.execute(query)).scalars().all()
    sample = [{"subject": row.subject or "(no subject)", "from": row.sender} for row in rows[:8]]
    if not confirm:
        return {"action": action, "count": len(rows), "sample": sample, "confirm": False}
    grouped: dict[Any, list[InboxItem]] = defaultdict(list)
    for row in rows:
        grouped[row.connector_id].append(row)
    deleted = 0
    for connector_id, items in grouped.items():
        runtime = await connector_for(db, user, connector_id)
        if runtime is None:
            continue
        await runtime.call(
            "bulk_delete",
            {"message_ids": [item.message_id for item in items], "permanent": False, "dry_run": False},
        )
        for item in items:
            item.status = "discarded"
            item.unread = False
            item.seen = True
            deleted += 1
    await db.commit()
    return {"action": action, "count": deleted, "deleted": deleted, "confirm": True, "sample": sample}


async def _ingest(
    db: AsyncSession,
    user: User,
    runtimes: list[RuntimeConnector],
    *,
    unread_only: bool,
    classify: bool,
) -> int:
    lock = _INGEST_LOCKS.setdefault(user.id, asyncio.Lock())
    if lock.locked():
        return 0
    async with lock:
        return await _ingest_locked(db, user, runtimes, unread_only=unread_only, classify=classify)


async def _ingest_locked(
    db: AsyncSession,
    user: User,
    runtimes: list[RuntimeConnector],
    *,
    unread_only: bool,
    classify: bool,
) -> int:
    arrived = 0
    pending: list[InboxItem] = []
    for runtime in runtimes:
        try:
            listed = (await runtime.call("list_messages", {"unread_only": unread_only, "limit": 40})).get("messages") or []
        except Exception as exc:  # noqa: BLE001
            log.warning("inbox list failed for %s: %s", runtime.slug, exc)
            continue
        mids = [msg.get("message_id") for msg in listed if msg.get("message_id")]
        existing: dict[str, InboxItem] = {}
        if mids:
            existing = {
                item.message_id: item
                for item in (
                    await db.execute(select(InboxItem).where(InboxItem.user_id == user.id, InboxItem.message_id.in_(mids)))
                ).scalars()
            }
        for msg in listed:
            mid = msg.get("message_id")
            if not mid:
                continue
            row = existing.get(mid)
            if row is None:
                row = InboxItem(
                    user_id=user.id,
                    connector_id=runtime.id,
                    message_id=mid,
                    thread_id=msg.get("thread_id") or "",
                    subject=(msg.get("subject") or "")[:500],
                    sender=(msg.get("from") or "")[:400],
                    to_header=(msg.get("to") or "")[:1000],
                    cc_header=(msg.get("cc") or "")[:1000],
                    recipient_role=(msg.get("recipient_role") or "to")[:12],
                    received_at=(msg.get("date") or "")[:80],
                    snippet=clip(msg.get("snippet") or "", 400),
                    unread=bool(msg.get("unread", True)),
                    seen=False,
                    status="pending",
                )
                db.add(row)
                existing[mid] = row
                arrived += 1
            else:
                row.unread = bool(msg.get("unread", row.unread))
                row.subject = (msg.get("subject") or row.subject)[:500]
                row.sender = (msg.get("from") or row.sender)[:400]
                if msg.get("to"):
                    row.to_header = str(msg.get("to") or "")[:1000]
                if msg.get("cc"):
                    row.cc_header = str(msg.get("cc") or "")[:1000]
                if msg.get("recipient_role"):
                    row.recipient_role = str(msg.get("recipient_role") or "to")[:12]
                row.received_at = (msg.get("date") or row.received_at)[:80]
                row.snippet = clip(msg.get("snippet") or row.snippet, 400)
                if not row.thread_id:
                    row.thread_id = msg.get("thread_id") or ""
            if classify and row.status == "pending" and not row.classification:
                pending.append(row)
    await db.commit()
    kept = await _trim_pending(db, user)
    kept_ids = {item.id for item in kept}
    pending = [item for item in pending if item.id in kept_ids]
    if classify and pending:
        await _classify_many(db, user, runtimes, pending[:MAX_CLASSIFY])
    return arrived


async def _classify_many(db: AsyncSession, user: User, runtimes: list[RuntimeConnector], items: list[InboxItem]) -> None:
    voice = await _voice(db, user)
    by_id = {r.id: r for r in runtimes}
    sem = asyncio.Semaphore(3)

    async def one(item: InboxItem) -> None:
        runtime = by_id.get(item.connector_id)
        if runtime is None:
            return
        async with sem:
            try:
                await _classify_one(item, runtime, voice)
            except Exception:  # noqa: BLE001
                log.warning("triage failed for %s", item.message_id, exc_info=True)

    await asyncio.gather(*[one(item) for item in items])
    await db.commit()


async def _classify_one(item: InboxItem, runtime: RuntimeConnector, voice: str) -> None:
    thread: dict[str, Any] = {}
    try:
        thread = await runtime.call(
            "get_thread",
            {"thread_id": item.thread_id or "", "message_id": item.message_id, "max_messages": 50},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("get_thread failed: %s", exc)
    if not isinstance(thread, dict) or not (thread.get("messages") or thread.get("body")):
        try:
            thread = await runtime.call("get_message", {"message_id": item.message_id})
        except Exception as exc:  # noqa: BLE001
            log.warning("get_message failed: %s", exc)
            thread = {"subject": item.subject, "from": item.sender, "body": item.snippet}
        tid = (thread.get("thread_id") or "").strip() if isinstance(thread, dict) else ""
        if tid and tid != item.thread_id:
            try:
                richer = await runtime.call(
                    "get_thread",
                    {"thread_id": tid, "message_id": item.message_id, "max_messages": 50},
                )
                if isinstance(richer, dict) and richer.get("messages"):
                    thread = richer
            except Exception as exc:  # noqa: BLE001
                log.warning("get_thread retry failed: %s", exc)
    if not item.subject:
        item.subject = (thread.get("subject") or item.subject)[:500]
    if not item.thread_id:
        item.thread_id = thread.get("thread_id") or ""
    if thread.get("to") and not getattr(item, "to_header", ""):
        item.to_header = str(thread.get("to") or "")[:1000]
    if thread.get("cc") and not getattr(item, "cc_header", ""):
        item.cc_header = str(thread.get("cc") or "")[:1000]
    latest_msg = (thread.get("messages") or [None])[-1] if isinstance(thread.get("messages"), list) else None
    if isinstance(latest_msg, dict):
        if latest_msg.get("to"):
            item.to_header = str(latest_msg.get("to") or item.to_header)[:1000]
        if latest_msg.get("cc"):
            item.cc_header = str(latest_msg.get("cc") or item.cc_header)[:1000]
    payload = {
        "subject": item.subject or thread.get("subject"),
        "from": item.sender,
        "to": getattr(item, "to_header", "") or thread.get("to") or "",
        "cc": getattr(item, "cc_header", "") or thread.get("cc") or "",
        "recipient_role": getattr(item, "recipient_role", "") or "to",
        "date": item.received_at,
        "latest": clip(thread.get("body") or item.snippet, 2500),
        "thread": [
            {
                "from": m.get("from"),
                "date": m.get("date"),
                "body": clip(m.get("body") or "", 2000),
            }
            for m in (thread.get("messages") or [])[-40:]
        ],
    }
    prompt = (
        "You triage inbound email for a busy professional. Use the full thread and follow-ups. "
        "Return JSON only with keys classification, summary, key_points, draft.\n"
        "classification must be one of: reply, respond, discard.\n"
        "- reply: a person asked a question or expects a written answer.\n"
        "- respond: needs confirmation, a decision, a booking, or a short acknowledgement.\n"
        "- discard: automated, marketing, quarantine, newsletter, no-reply, or no action needed.\n"
        "recipient_role is to, cc, or bcc. Mail CC'd or BCC'd to the user still belongs in their inbox. "
        "Do not discard it only because they are not the primary To recipient. "
        "If they were asked to act or stay informed, classify reply or respond and say they were CC'd.\n"
        "summary: 2-4 sentences covering the latest message and prior thread context.\n"
        "key_points: 2-5 short bullets the user should see.\n"
        "draft: a ready-to-send reply in the user's voice that answers open questions from the whole thread. "
        "If classification is discard, draft a one-line note that no reply is needed — do not invent a fake email.\n"
        "Never invent facts, dates, or commitments. Do not include a subject line.\n"
        f"User writing voice: {clip(voice or 'plain, concise, professional', 800)}\n"
        f"Email JSON: {clip(json.dumps(payload, default=str), 30000)}\n"
    )
    apply_triage(item, parse_json_object(await _llm(prompt, 900, json_mode=True)))


async def _voice(db: AsyncSession, user: User) -> str:
    """Inbox drafts use Chief's voice. Other agents keep their own blocks."""
    from app.agent.memory import read_core

    return await read_core(db, user, "voice")


def _thread_as_messages(data: dict[str, Any]) -> dict[str, Any]:
    msgs = data.get("messages") if isinstance(data, dict) else None
    if msgs:
        return {
            "thread_id": data.get("thread_id") or "",
            "subject": data.get("subject") or "",
            "message_count": data.get("message_count") or len(msgs),
            "last_message_id": data.get("last_message_id"),
            "messages": [
                {
                    "message_id": m.get("message_id"),
                    "from": m.get("from"),
                    "to": m.get("to"),
                    "cc": m.get("cc"),
                    "date": m.get("date"),
                    "subject": m.get("subject") or data.get("subject"),
                    "body": m.get("body") or "",
                    "has_attachments": bool(m.get("has_attachments") or m.get("attachments")),
                }
                for m in msgs
                if isinstance(m, dict)
            ],
        }
    return {
        "thread_id": data.get("thread_id") or "",
        "subject": data.get("subject") or "",
        "message_count": 1 if data.get("body") else 0,
        "last_message_id": data.get("message_id"),
        "messages": [
            {
                "message_id": data.get("message_id"),
                "from": data.get("from"),
                "to": data.get("to"),
                "cc": data.get("cc"),
                "date": data.get("date"),
                "subject": data.get("subject"),
                "body": data.get("body") or data.get("snippet") or "",
                "has_attachments": bool(data.get("has_attachments") or data.get("attachments")),
            }
        ]
        if data.get("body") or data.get("snippet")
        else [],
    }


async def open_full_thread(
    db: AsyncSession,
    user: User,
    *,
    slug: str = "",
    thread_id: str = "",
    message_id: str = "",
    item_id=None,
) -> dict[str, Any]:
    runtimes = await mail_connectors(db, user)
    runtime = None
    if item_id is not None:
        item = await get_item(db, user, item_id)
        runtime = next((r for r in runtimes if r.id == item.connector_id), None)
        thread_id = thread_id or item.thread_id or ""
        message_id = message_id or item.message_id or ""
        slug = slug or (runtime.slug if runtime else "")
    if runtime is None and slug:
        runtime = next((r for r in runtimes if r.slug == slug or r.provider == slug), None)
    if runtime is None and len(runtimes) == 1:
        runtime = runtimes[0]
    if runtime is None:
        raise LookupError("Mail connector not found")
    if not thread_id and not message_id:
        raise ValueError("thread_id or message_id is required")
    try:
        data = await runtime.call(
            "get_thread",
            {"thread_id": thread_id or "", "message_id": message_id or "", "max_messages": 100, "full": True},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("open_full_thread get_thread failed: %s", exc)
        data = {}
    packed = _thread_as_messages(data if isinstance(data, dict) else {})
    if packed["messages"]:
        packed["connector_slug"] = runtime.slug
        return packed
    if message_id:
        try:
            msg = await runtime.call("get_message", {"message_id": message_id})
        except Exception as exc:  # noqa: BLE001
            raise ValueError(str(exc)[:300]) from exc
        packed = _thread_as_messages(msg if isinstance(msg, dict) else {})
    packed["connector_slug"] = runtime.slug
    if not packed["messages"]:
        raise LookupError("Could not load that email thread")
    return packed


async def _llm(prompt: str, max_tokens: int, json_mode: bool = False) -> str:
    kwargs: dict[str, Any] = {}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        resp = await chat_complete([{"role": "user", "content": prompt}], max_tokens=max_tokens, **kwargs)
        return (resp.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("inbox llm failed: %s", exc)
        return ""
