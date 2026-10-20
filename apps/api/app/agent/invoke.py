from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from app.agent.attachments import hydrate_attachments, process_message_result, store_blob
from app.agent.staff import STAFF_TOOLS, dispatch as staff_dispatch
from app.agent.summarize import summarize_thread
from app.config import get_settings
from app.connectors.runtime import RuntimeConnector, is_binary
from app.models import ToolUsage
from connector_core.calendar import DEFAULT_TZ, as_local, format_local, tz_label
from connector_core.text import clip

log = logging.getLogger(__name__)


def confirmed(confirmations: list[dict], slug: str, tool: str) -> bool:
    return any(c.get("slug") == slug and c.get("tool") == tool for c in confirmations)


async def preview(user, conn, tool: str, args: dict) -> dict:
    if tool == "bulk_delete":
        preview_result = await RuntimeConnector(conn).call("bulk_delete", {**args, "dry_run": True})
        return {
            "count": preview_result.get("count"),
            "sample": preview_result.get("sample"),
            "permanent": args.get("permanent", False),
        }
    if tool in {"create_event", "create_google_meet", "create_teams_meeting"}:
        tz = args.get("timezone") or getattr(user, "timezone", None) or DEFAULT_TZ
        start = args.get("start")
        end = args.get("end")
        duration = args.get("duration_minutes") or 30
        when = None
        if start:
            end_dt = as_local(end, tz) if end else as_local(start, tz) + timedelta(minutes=duration)
            when = f"{format_local(start, tz)} – {format_local(end_dt, tz)}"
        out = {
            "title": args.get("title"),
            "when": when,
            "timezone": f"{tz_label(tz)} ({DEFAULT_TZ})" if tz_label(tz) == "IST" else tz,
        }
        for key in ("attendees", "location"):
            if args.get(key) not in (None, "", []):
                out[key] = args.get(key)
        return {k: v for k, v in out.items() if v not in (None, "", [])}
    return {k: args.get(k) for k in ("to", "subject", "body", "message_id", "query") if args.get(k)}


async def invoke_named(db, user, conn, tool_name: str, args: dict, attachments: list) -> Any:
    runtime = RuntimeConnector(conn)
    if tool_name in STAFF_TOOLS:
        return await staff_dispatch(db, user, runtime, tool_name, args)
    if tool_name in {"create_event", "create_google_meet", "create_teams_meeting"}:
        args = {**args, "timezone": args.get("timezone") or user.timezone or DEFAULT_TZ}
    if tool_name == "summarize_thread":
        thread = await runtime.call("get_thread", {"thread_id": args["thread_id"]})
        return await summarize_thread(db, conn.id, user.id, thread, args.get("focus"))
    if tool_name == "summarize_attachments":
        listed = (await runtime.call("list_attachments", {"message_id": args["message_id"]})).get("attachments") or []
        cards = await hydrate_attachments(
            user, runtime, args["message_id"], listed, attachments, summarize=True, focus=args.get("focus")
        )
        return {
            "message_id": args["message_id"],
            "attachment_summary": next((c.get("message_summary") for c in cards if c.get("message_summary")), ""),
            "attachments": cards,
        }
    if tool_name == "read_attachment_text":
        blob = await runtime.call("download_attachment", args)
        if not is_binary(blob):
            return {"error": "not a file"}
        card = await store_blob(user.id, blob)
        attachments.append(card)
        return {
            "filename": card["filename"],
            "text": clip(card.get("extracted_text") or "", 4000) or "No extractable text.",
            "download_url": card["download_url"],
            "preview_url": card.get("preview_url"),
        }
    if tool_name == "download_attachment":
        blob = await runtime.call("download_attachment", args)
        if not is_binary(blob):
            return blob
        if len(blob.data) > get_settings().attachment_max_bytes:
            return {"error": "Attachment is larger than the 25 MB limit"}
        card = await store_blob(user.id, blob)
        attachments.append(card)
        return {k: card[k] for k in ("filename", "download_url", "preview_url", "size", "kind") if k in card}
    if tool_name == "bulk_delete":
        args = {**args, "dry_run": False}
    result = await runtime.call(tool_name, args)
    if tool_name in {"get_message", "list_attachments"}:
        result = await process_message_result(user, runtime, result, attachments, summarize=False)
    if tool_name == "get_thread" and isinstance(result, dict):
        msgs = result.get("messages") or []
        last = next((m for m in reversed(msgs) if m.get("attachments") or m.get("has_attachments")), None)
        if last and last.get("message_id"):
            listed = last.get("attachments")
            if not listed or (listed and isinstance(listed[0], str)):
                listed = (await runtime.call("list_attachments", {"message_id": last["message_id"]})).get("attachments") or []
            if listed:
                result["message_id"] = last["message_id"]
                result["attachments"] = listed
                result = await process_message_result(user, runtime, result, attachments, summarize=False)
    return result


async def bump_usage(db, user_id, connector_id, tool: str, ok: bool) -> None:
    try:
        row = (
            await db.execute(
                select(ToolUsage).where(
                    ToolUsage.user_id == user_id,
                    ToolUsage.connector_id == connector_id,
                    ToolUsage.tool_name == tool,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = ToolUsage(user_id=user_id, connector_id=connector_id, tool_name=tool, success=0, failure=0)
            db.add(row)
        if ok:
            row.success = (row.success or 0) + 1
        else:
            row.failure = (row.failure or 0) + 1
        await db.flush()
    except Exception:  # noqa: BLE001
        log.warning("usage counter failed", exc_info=True)
