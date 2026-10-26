from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import chat_complete
from app.models import AdminItem, Automation, Commitment, ScheduledTask
from connector_core.text import clip

log = logging.getLogger(__name__)

STAFF_TOOLS = {
    "today_briefing",
    "ask_inbox",
    "scan_commitments",
    "list_commitments",
    "scan_admin",
    "schedule_task",
    "list_scheduled_tasks",
    "update_scheduled_task",
    "create_automation",
    "list_automations",
    "learn_voice",
}


def _now(tz: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(tz or "Asia/Kolkata"))
    except Exception:  # noqa: BLE001
        return datetime.now(ZoneInfo("Asia/Kolkata"))


async def dispatch(db: AsyncSession, user, runtime, tool: str, args: dict, agent=None) -> dict:
    if tool == "today_briefing":
        return await today_briefing(db, user, runtime, agent=agent)
    if tool == "ask_inbox":
        return await ask_inbox(runtime, args.get("question") or "")
    if tool == "scan_commitments":
        return await scan_commitments(db, user, runtime)
    if tool == "list_commitments":
        return await list_commitments(db, user.id)
    if tool == "scan_admin":
        return await scan_admin(db, user, runtime)
    if tool == "schedule_task":
        return await schedule_task(db, user, args, agent=agent)
    if tool == "list_scheduled_tasks":
        return await list_scheduled_tasks(db, user, agent=agent)
    if tool == "update_scheduled_task":
        return await update_scheduled_task(db, user, args)
    if tool == "create_automation":
        return await create_automation(db, user, args, agent=agent)
    if tool == "list_automations":
        return await list_automations(db, user.id, agent_id=getattr(agent, "id", None) if agent is not None else None)
    if tool == "learn_voice":
        return await learn_voice(db, user, runtime, agent=agent)
    return {"error": f"unknown staff tool {tool}"}


async def today_briefing(db, user, runtime, agent=None) -> dict:
    tz = user.timezone or "Asia/Kolkata"
    now = _now(tz)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    events = []
    unread = []
    try:
        events = (await runtime.call("list_events", {"start": start.isoformat(), "end": end.isoformat(), "limit": 12})).get(
            "events"
        ) or []
    except Exception as exc:  # noqa: BLE001
        log.warning("briefing events failed: %s", exc)
    try:
        unread = (await runtime.call("list_messages", {"unread_only": True, "limit": 20})).get("messages") or []
    except Exception as exc:  # noqa: BLE001
        log.warning("briefing unread failed: %s", exc)
    commits = (
        await db.execute(
            select(Commitment).where(Commitment.user_id == user.id, Commitment.status == "open").order_by(Commitment.id.desc()).limit(12)
        )
    ).scalars().all()
    admin = (await db.execute(select(AdminItem).where(AdminItem.user_id == user.id).order_by(AdminItem.id.desc()).limit(8))).scalars().all()
    auto_stmt = select(Automation).where(Automation.user_id == user.id, Automation.enabled.is_(True))
    task_stmt = (
        select(ScheduledTask)
        .where(ScheduledTask.user_id == user.id, ScheduledTask.status.in_(("active", "running")))
        .order_by(ScheduledTask.next_run_at.asc())
        .limit(8)
    )
    owner_id = getattr(agent, "id", None) if agent is not None else None
    if owner_id is not None:
        auto_stmt = auto_stmt.where(Automation.agent_id == owner_id)
        task_stmt = task_stmt.where(ScheduledTask.agent_id == owner_id)
    autos = (await db.execute(auto_stmt)).scalars().all()
    scheduled = (await db.execute(task_stmt)).scalars().all()
    payload = {
        "timezone": tz,
        "now": now.isoformat(),
        "calendar": events,
        "needs_response": [
            {
                "message_id": m.get("message_id"),
                "subject": m.get("subject"),
                "from": m.get("from"),
                "to": m.get("to"),
                "cc": m.get("cc"),
                "recipient_role": m.get("recipient_role") or "to",
                "date": m.get("date"),
            }
            for m in unread
        ],
        "commitments": [_commit(c) for c in commits],
        "admin": [_admin(a) for a in admin],
        "automations": [{"name": a.name, "trigger": a.trigger, "action": a.action} for a in autos],
        "scheduled_tasks": [
            {
                "title": t.title,
                "schedule": t.schedule_label,
                "next_run": t.next_run_at.isoformat() if t.next_run_at else None,
                "status": t.status,
            }
            for t in scheduled
        ],
    }
    prompt = (
        "You are the user's chief of staff and analyst. Write a detailed daily briefing from this JSON, not a short recap. "
        "Sections: What matters today; Calendar risk; People waiting on a reply (cite subject + message_id); "
        "CC'd to you (recipient_role is cc or bcc — still include these, do not skip them); "
        "Open commitments; Admin/bills; Insights and risks; Next actions with why they matter. "
        "Mail where the user is on Cc or Bcc is part of their inbox. Cover it. "
        "Show every time in IST (Asia/Kolkata). Do not invent items.\n\n"
        + clip(json.dumps(payload, default=str), 10000)
    )
    text = await _llm(prompt, 1400)
    return {"briefing": text, **payload}


async def ask_inbox(runtime, question: str) -> dict:
    if not question.strip():
        return {"error": "question is required"}
    found = (await runtime.call("search_messages", {"query": question, "limit": 8})).get("messages") or []
    bodies = []
    for m in found[:5]:
        mid = m.get("message_id")
        if not mid:
            continue
        try:
            full = await runtime.call("get_message", {"message_id": mid})
        except Exception:  # noqa: BLE001
            full = m
        bodies.append(
            {
                "message_id": mid,
                "thread_id": full.get("thread_id") or m.get("thread_id"),
                "subject": full.get("subject") or m.get("subject"),
                "from": full.get("from") or m.get("from"),
                "to": full.get("to") or m.get("to"),
                "cc": full.get("cc") or m.get("cc"),
                "recipient_role": full.get("recipient_role") or m.get("recipient_role") or "to",
                "date": full.get("date") or m.get("date"),
                "body": clip(full.get("body") or m.get("snippet") or "", 1200),
            }
        )
    prompt = (
        f"Answer this question from the user's email, including messages they were CC'd or BCC'd on. "
        f"Cite message_id, subject, and recipient_role for every claim. If unknown, say so.\n"
        f"Question: {question}\n\nEmails:\n{clip(json.dumps(bodies, default=str), 9000)}"
    )
    return {"answer": await _llm(prompt, 600), "sources": bodies}


async def scan_commitments(db, user, runtime) -> dict:
    msgs = (await runtime.call("list_messages", {"limit": 20})).get("messages") or []
    prompt = (
        "Extract open commitments, promises, and pending decisions from these emails. "
        'Return JSON {"items":[{"text":"","counterparty":"","due":"ISO or empty","message_id":"","thread_id":""}]}.\n'
        + clip(json.dumps(msgs, default=str), 8000)
    )
    raw = await _llm(prompt, 500, json_mode=True)
    items = _json_items(raw)
    saved = []
    for item in items[:20]:
        row = Commitment(
            user_id=user.id,
            connector_id=getattr(runtime, "id", None),
            text=(item.get("text") or "")[:500],
            counterparty=(item.get("counterparty") or "")[:320],
            message_id=item.get("message_id") or "",
            thread_id=item.get("thread_id") or "",
            status="open",
        )
        if item.get("due"):
            try:
                from connector_core.calendar import parse_iso

                row.due_at = parse_iso(item["due"])
            except Exception:  # noqa: BLE001
                pass
        if row.text:
            db.add(row)
            saved.append(item)
    await db.commit()
    return {"added": len(saved), "items": saved}


async def list_commitments(db, user_id) -> dict:
    rows = (
        await db.execute(select(Commitment).where(Commitment.user_id == user_id, Commitment.status == "open").order_by(Commitment.id.desc()).limit(30))
    ).scalars().all()
    return {"commitments": [_commit(r) for r in rows]}


async def scan_admin(db, user, runtime) -> dict:
    found = (await runtime.call("search_messages", {"query": "invoice OR receipt OR subscription OR bill OR renewal", "limit": 15})).get(
        "messages"
    ) or []
    prompt = (
        "Extract receipts, invoices, bills, and subscription renewals. "
        'Return JSON {"items":[{"kind":"invoice|receipt|bill|subscription","vendor":"","amount":"","due":"","message_id":"","notes":""}]}.\n'
        + clip(json.dumps(found, default=str), 8000)
    )
    items = _json_items(await _llm(prompt, 500, json_mode=True))
    saved = []
    for item in items[:20]:
        row = AdminItem(
            user_id=user.id,
            kind=(item.get("kind") or "invoice")[:40],
            vendor=(item.get("vendor") or "")[:200],
            amount=(item.get("amount") or "")[:40],
            message_id=item.get("message_id") or "",
            notes=(item.get("notes") or "")[:500],
        )
        db.add(row)
        saved.append(item)
    await db.commit()
    return {"added": len(saved), "items": saved}


async def schedule_task(db, user, args: dict, agent=None) -> dict:
    from app.tasks.service import create_from_prompt

    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return {"error": "prompt is required"}
    name = (args.get("agent_name") or "").strip()
    if name:
        from app.agents.service import find_agent_by_name

        named = await find_agent_by_name(db, user, name)
        if named is None:
            return {"error": f"No agent named {name}. Leave agent_name empty to schedule it on yourself."}
        agent = named
    try:
        task = await create_from_prompt(db, user, prompt, agent_id=getattr(agent, "id", None) if agent is not None else None)
    except ValueError as exc:
        return {"error": str(exc)}
    return {"created": True, "task": task}


async def list_scheduled_tasks(db, user, agent=None) -> dict:
    from app.tasks.service import list_tasks

    agent_id = getattr(agent, "id", None) if agent is not None else None
    return {"tasks": await list_tasks(db, user, agent_id=agent_id)}


async def update_scheduled_task(db, user, args: dict) -> dict:
    from app.tasks.service import set_status

    try:
        task_id = uuid.UUID(str(args.get("task_id") or ""))
        return await set_status(db, user, task_id, args.get("status") or "")
    except (KeyError, ValueError) as exc:
        return {"error": str(exc) if str(exc) != "task" else "task not found"}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:200]}


async def _routine_owner(db, user, agent):
    if agent is not None and getattr(agent, "id", None):
        return agent
    from app.agents.service import ensure_default_agent

    return await ensure_default_agent(db, user)


async def create_automation(db, user, args: dict, agent=None) -> dict:
    owner = await _routine_owner(db, user, agent)
    row = Automation(
        user_id=user.id,
        agent_id=owner.id,
        name=(args.get("name") or "Untitled")[:200],
        trigger=args.get("trigger") or "",
        condition=args.get("condition") or "",
        action=args.get("action") or "",
        requires_approval=bool(args.get("requires_approval", True)),
        enabled=True,
    )
    db.add(row)
    await db.commit()
    out = {
        "id": row.id,
        "name": row.name,
        "trigger": row.trigger,
        "action": row.action,
        "requires_approval": row.requires_approval,
        "agent_id": str(owner.id),
    }
    text = " ".join(part for part in (row.name, row.trigger, row.action) if part).strip()
    if text:
        try:
            from app.tasks.service import create_from_prompt

            scheduled = await create_from_prompt(db, user, text, require_time=True, agent_id=owner.id)
            if scheduled:
                out["scheduled_task"] = scheduled
        except Exception as exc:  # noqa: BLE001
            out["schedule_note"] = str(exc)[:200]
    return out


async def list_automations(db, user_id, agent_id=None) -> dict:
    stmt = select(Automation).where(Automation.user_id == user_id).order_by(Automation.id.desc())
    if agent_id is not None:
        stmt = stmt.where(Automation.agent_id == agent_id)
    rows = (await db.execute(stmt)).scalars().all()
    return {
        "automations": [
            {
                "id": r.id,
                "name": r.name,
                "trigger": r.trigger,
                "condition": r.condition,
                "action": r.action,
                "requires_approval": r.requires_approval,
                "enabled": r.enabled,
                "agent_id": str(r.agent_id) if r.agent_id else None,
            }
            for r in rows
        ]
    }


async def learn_voice(db, user, runtime, agent=None) -> dict:
    sent = (await runtime.call("search_messages", {"query": "in:sent", "limit": 12})).get("messages") or []
    if not sent:
        sent = (await runtime.call("list_messages", {"folder": "sent", "limit": 12})).get("messages") or []
    prompt = (
        "Describe this person's email voice in 8 short bullets: tone, greeting, sign-off, how they address clients vs teammates, "
        "formality, typical length, phrases to use, phrases to avoid. This becomes a writing twin.\n"
        + clip(json.dumps(sent, default=str), 8000)
    )
    voice = await _llm(prompt, 400)
    from app.agent.memory import upsert_core

    owner = await _routine_owner(db, user, agent)
    await upsert_core(db, user.id, "voice", voice, owner.id)
    return {"voice": voice}


def _commit(c: Commitment) -> dict:
    return {
        "id": c.id,
        "text": c.text,
        "counterparty": c.counterparty,
        "due_at": c.due_at.isoformat() if c.due_at else None,
        "message_id": c.message_id,
        "status": c.status,
    }


def _admin(a: AdminItem) -> dict:
    return {"kind": a.kind, "vendor": a.vendor, "amount": a.amount, "due_at": a.due_at.isoformat() if a.due_at else None, "message_id": a.message_id, "notes": a.notes}


def _json_items(raw: str) -> list[dict]:
    try:
        data = json.loads(raw)
        items = data.get("items") if isinstance(data, dict) else data
        return [i for i in (items or []) if isinstance(i, dict)]
    except Exception:  # noqa: BLE001
        return []


async def _llm(prompt: str, max_tokens: int, json_mode: bool = False) -> str:
    kwargs = {}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        resp = await chat_complete([{"role": "user", "content": prompt}], max_tokens=max_tokens, **kwargs)
        return (resp.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("staff llm failed: %s", exc)
        return ""
