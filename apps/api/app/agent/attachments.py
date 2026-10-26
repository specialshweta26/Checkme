from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from app.cache import get_redis
from app.config import get_settings
from app.connectors.runtime import RuntimeConnector, is_binary
from app.llm import chat_complete
from app.storage import extract_text, put_bytes, signed_url
from connector_core.text import clip

log = logging.getLogger(__name__)

PREVIEW_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "text/plain",
    "text/csv",
    "text/html",
}
MAX_AUTO = 8


def is_previewable(filename: str, content_type: str) -> bool:
    name = (filename or "").lower()
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in PREVIEW_TYPES:
        return True
    return name.endswith((".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".txt", ".csv", ".html"))


def _kind(filename: str, content_type: str) -> str:
    name = (filename or "").lower()
    ctype = (content_type or "").lower()
    if "pdf" in ctype or name.endswith(".pdf"):
        return "pdf"
    if ctype.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
        return "image"
    if name.endswith(".html") or "html" in ctype:
        return "html"
    if name.endswith((".txt", ".csv", ".md")) or ctype.startswith("text/"):
        return "text"
    if name.endswith(".docx"):
        return "docx"
    return "file"


def _cache_key(user_id, message_id: str, attachment_id: str) -> str:
    return f"att:{user_id}:{message_id}:{attachment_id}"


async def cache_attachment(user_id, message_id: str, attachment_id: str, card: dict) -> None:
    await get_redis().set(
        _cache_key(user_id, message_id, attachment_id),
        json.dumps(
            {
                "filename": card.get("filename"),
                "extracted_text": card.get("extracted_text") or "",
                "download_url": card.get("download_url"),
                "preview_url": card.get("preview_url"),
                "kind": card.get("kind"),
            }
        ),
        ex=3600,
    )


async def cached_attachment(user_id, message_id: str, attachment_id: str) -> dict | None:
    raw = await get_redis().get(_cache_key(user_id, message_id, attachment_id))
    return json.loads(raw) if raw else None


async def store_blob(user_id, blob, message_id: str = "", attachment_id: str = "") -> dict:
    import re

    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", blob.filename or "file")[:180] or "file"
    key = f"{user_id}/{uuid.uuid4().hex}/{safe_name}"
    put_bytes(key, blob.data, blob.content_type)
    download = signed_url(key, blob.filename, inline=False, content_type=blob.content_type)
    preview = (
        signed_url(key, blob.filename, inline=True, content_type=blob.content_type)
        if is_previewable(blob.filename, blob.content_type)
        else None
    )
    text = extract_text(blob.filename, blob.content_type, blob.data, limit=8000)
    card = {
        "filename": blob.filename,
        "content_type": blob.content_type,
        "size": len(blob.data),
        "kind": _kind(blob.filename, blob.content_type),
        "url": download,
        "download_url": download,
        "preview_url": preview,
        "previewable": bool(preview),
        "extracted_text": clip(text, 4000) if text else "",
        "message_id": message_id,
        "attachment_id": attachment_id,
    }
    if message_id and attachment_id:
        await cache_attachment(user_id, message_id, attachment_id, card)
    return card


async def summarize_texts(items: list[dict], focus: str | None = None) -> str:
    parts = [f"### {i['filename']}\n{i.get('extracted_text') or '(no extractable text)'}" for i in items]
    if not any(i.get("extracted_text") for i in items):
        names = ", ".join(i["filename"] for i in items)
        return f"No extractable text in: {names}. Use the preview or download links."
    prompt = (
        "You are a senior analyst. Write a detailed briefing of these files, not a short summary. "
        "For each file cover: what it is, the important numbers and dates, trends or exceptions, "
        "risks or decisions, and what the user should do next. Quote figures exactly. "
        f"Focus: {focus or 'overall'}\n\n" + clip("\n\n".join(parts), 16000)
    )
    try:
        resp = await chat_complete([{"role": "user", "content": prompt}], max_tokens=1800)
        return (resp.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("attachment summary failed: %s", exc)
        return clip("\n".join(p[:400] for p in parts), 800)


async def hydrate_attachments(
    user,
    runtime: RuntimeConnector,
    message_id: str,
    listed: list[dict],
    out_cards: list[dict],
    *,
    summarize: bool = True,
    focus: str | None = None,
) -> list[dict]:
    limit = get_settings().attachment_max_bytes
    processed = []
    for meta in (listed or [])[:MAX_AUTO]:
        att_id = meta.get("attachment_id") or meta.get("id")
        if not att_id:
            processed.append({**meta, "error": "missing attachment_id"})
            continue
        try:
            blob = await runtime.call(
                "download_attachment", {"message_id": message_id, "attachment_id": att_id}
            )
            if not is_binary(blob):
                processed.append({**meta, "error": "not a file"})
                continue
            if len(blob.data) > limit:
                processed.append({**meta, "filename": blob.filename, "error": "larger than 25 MB"})
                continue
            card = await store_blob(user.id, blob, message_id, att_id)
            card["connector_slug"] = getattr(runtime, "slug", None)
            processed.append(card)
            public = {k: card[k] for k in card if k != "extracted_text"}
            out_cards.append(public)
        except Exception as exc:  # noqa: BLE001
            log.warning("attachment hydrate failed: %s", exc)
            processed.append({**meta, "error": str(exc)[:200]})
    if summarize and processed:
        summary = await summarize_texts([p for p in processed if p.get("filename")], focus)
        for p in processed:
            p["summary"] = summary if p.get("extracted_text") is not None else p.get("error")
        processed = [{**p, "message_summary": summary} for p in processed]
    return processed


async def summarize_one(user, runtime: RuntimeConnector, message_id: str, attachment_id: str) -> dict:
    cached = await cached_attachment(user.id, message_id, attachment_id)
    if cached is None:
        blob = await runtime.call("download_attachment", {"message_id": message_id, "attachment_id": attachment_id})
        if not is_binary(blob):
            return {"error": "not a file"}
        cached = await store_blob(user.id, blob, message_id, attachment_id)
    summary = await summarize_texts([{"filename": cached.get("filename") or "file", "extracted_text": cached.get("extracted_text") or ""}], None)
    return {
        "filename": cached.get("filename"),
        "summary": summary,
        "download_url": cached.get("download_url"),
        "preview_url": cached.get("preview_url"),
    }


async def process_message_result(
    user, runtime: RuntimeConnector, result: Any, out_cards: list[dict], summarize: bool = True
) -> Any:
    if not isinstance(result, dict):
        return result
    message_id = result.get("message_id")
    listed = result.get("attachments")
    if not message_id or not listed:
        return result
    hydrated = await hydrate_attachments(user, runtime, message_id, listed, out_cards, summarize=summarize)
    result["attachments"] = [
        {
            "attachment_id": h.get("attachment_id"),
            "message_id": h.get("message_id") or message_id,
            "connector_slug": h.get("connector_slug") or getattr(runtime, "slug", None),
            "filename": h.get("filename"),
            "content_type": h.get("content_type"),
            "size": h.get("size"),
            "kind": h.get("kind"),
            "download_url": h.get("download_url"),
            "preview_url": h.get("preview_url"),
            "previewable": h.get("previewable"),
            "error": h.get("error"),
        }
        for h in hydrated
    ]
    if summarize:
        summaries = [h.get("message_summary") for h in hydrated if h.get("message_summary")]
        if summaries:
            result["attachment_summary"] = summaries[0]
    return result
