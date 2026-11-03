"""Save Gmail and Outlook attachments into a folder on the user's computer.

Search pages are 50 messages. This keeps paging, downloads the file bytes, and writes them.
A chat download link is not a substitute for that folder.
"""

from __future__ import annotations

import re
from typing import Any

from app.config import get_settings
from app.connectors.runtime import RuntimeConnector, is_binary

_MAIL = {"gmail", "outlook"}
_PAGE = 50
_MAX_MESSAGES = 200


def mail_accounts(connectors, account: str = "") -> list:
    """Connected Gmail and Outlook rows. ``account`` limits to the one the user named."""
    want = (account or "").strip().lower()
    rows = []
    items = connectors.items() if hasattr(connectors, "items") else []
    for slug, conn in items:
        provider = (getattr(conn, "provider", "") or "").lower()
        name = (getattr(conn, "name", "") or "").lower()
        key = provider if provider in _MAIL else (slug if slug in _MAIL else "")
        if not key:
            continue
        if want and want not in {key, slug.lower(), name}:
            continue
        rows.append(conn)
    return rows


def _search_query(provider: str, query: str) -> str:
    text = (query or "").strip()
    if provider == "gmail" and "has:attachment" not in text.lower():
        return f"{text} has:attachment".strip()
    return text


def _filename(raw: str, used: set[str]) -> str:
    name = re.sub(r"[\x00-\x1f/\\]", "_", str(raw or "attachment")).strip().strip(".") or "attachment"
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    candidate = name
    n = 2
    while candidate.lower() in used:
        candidate = f"{stem}-{n}.{ext}" if ext else f"{stem}-{n}"
        n += 1
    used.add(candidate.lower())
    return candidate


def _join(folder: str, name: str) -> str:
    base = (folder or "").rstrip("/\\")
    if "\\" in base and "/" not in base:
        return base + "\\" + name
    return base + "/" + name


async def save_mail_attachments(
    ctx,
    query: str,
    path: str,
    account: str = "",
    page_token: str = "",
    *,
    max_messages: int = _MAX_MESSAGES,
    write_bytes=None,
) -> dict:
    """Page every connected mail account, download attachments, and write them under ``path``."""
    folder = (path or "").strip()
    if not folder:
        return {"error": "path is required"}
    if not (query or "").strip():
        return {"error": "query is required"}
    accounts = mail_accounts(getattr(ctx, "connectors", {}), account)
    if not accounts:
        named = (account or "").strip()
        if named:
            return {"error": f"No connected {named} account. Connect Gmail or Outlook, then save again."}
        return {"error": "No Gmail or Outlook account is connected. Connect the mailbox, then save again."}
    if write_bytes is None:
        from app.agent.computer_harness import write_bytes as write_bytes

    limit = get_settings().attachment_max_bytes
    saved: list[dict] = []
    failed: list[dict] = []
    scanned: dict[str, int] = {}
    more: dict[str, str] = {}
    used: set[str] = set()
    token_in = (page_token or "").strip()
    for conn in accounts:
        provider = (getattr(conn, "provider", "") or getattr(conn, "slug", "")).lower()
        slug = getattr(conn, "slug", provider)
        runtime = conn if hasattr(conn, "call") else RuntimeConnector(conn)
        token = token_in if token_in and account else ""
        seen = 0
        next_token = ""
        while seen < max_messages:
            try:
                page = await runtime.call(
                    "search_messages",
                    {"query": _search_query(provider, query), "limit": _PAGE, "page_token": token or None},
                )
            except Exception as exc:  # noqa: BLE001
                failed.append({"account": slug, "error": str(exc)[:240]})
                break
            if not isinstance(page, dict):
                failed.append({"account": slug, "error": "search did not return messages"})
                break
            messages = page.get("messages") or []
            next_token = str(page.get("next_page_token") or "")
            if not messages:
                next_token = ""
                break
            for message in messages:
                seen += 1
                if provider == "outlook" and message.get("has_attachments") is False:
                    continue
                message_id = str(message.get("message_id") or "")
                if not message_id:
                    continue
                try:
                    listed = await runtime.call("list_attachments", {"message_id": message_id})
                except Exception as exc:  # noqa: BLE001
                    failed.append({"account": slug, "message_id": message_id, "error": str(exc)[:240]})
                    continue
                for item in (listed or {}).get("attachments") or []:
                    attachment_id = str(item.get("attachment_id") or "")
                    if not attachment_id:
                        continue
                    try:
                        blob = await runtime.call(
                            "download_attachment", {"message_id": message_id, "attachment_id": attachment_id}
                        )
                    except Exception as exc:  # noqa: BLE001
                        failed.append({"account": slug, "message_id": message_id, "error": str(exc)[:240]})
                        continue
                    if not is_binary(blob):
                        failed.append({"account": slug, "message_id": message_id, "error": "attachment was not a file"})
                        continue
                    if len(blob.data) > limit:
                        failed.append(
                            {
                                "account": slug,
                                "filename": blob.filename,
                                "error": "larger than the 25 MB limit",
                            }
                        )
                        continue
                    filename = _filename(blob.filename, used)
                    dest = _join(folder, filename)
                    written = await write_bytes(ctx, dest, blob.data)
                    if isinstance(written, dict) and written.get("need") == "overwrite_confirm":
                        filename = _filename(blob.filename, used)
                        dest = _join(folder, filename)
                        written = await write_bytes(ctx, dest, blob.data)
                    if isinstance(written, dict) and (written.get("ok") or written.get("verified")):
                        saved.append(
                            {
                                "account": slug,
                                "path": written.get("path") or dest,
                                "filename": filename,
                                "bytes": len(blob.data),
                                "subject": message.get("subject") or "",
                            }
                        )
                    else:
                        failed.append(
                            {
                                "account": slug,
                                "filename": filename,
                                "error": (written or {}).get("error") if isinstance(written, dict) else "write failed",
                            }
                        )
            token = next_token
            if not token or seen >= max_messages:
                break
        scanned[slug] = seen
        if next_token and seen >= max_messages:
            more[slug] = next_token
    return {
        "folder": folder,
        "saved": saved,
        "saved_count": len(saved),
        "failed": failed[:20],
        "scanned": scanned,
        "more": more,
        "note": (
            "Files were written into the folder. Gmail and Outlook use the same save. "
            "If more has a page token, call save_mail_attachments again with that account and page_token."
            if not more
            else "More matching mail remains. Call save_mail_attachments again with each account and page_token in more."
        ),
    }
