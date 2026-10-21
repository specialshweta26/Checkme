from __future__ import annotations

import base64
import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from connector_core import MAIL_CALENDAR_TOOLS, BaseConnector, BinaryResult, ToolError, TokenProvider
from connector_core.mail_tools import canonical_mail_folder, folder_from_query
from connector_core.calendar import (
    DEFAULT_TZ,
    as_local,
    as_utc,
    format_local,
    free_slots,
    graph_datetime,
    graph_timezone,
    now_local,
    parse_iso,
    tz_label,
)
from connector_core.http import ApiClient, gather_limited
from connector_core.text import clean_body, clip

GRAPH = "https://graph.microsoft.com/v1.0"
ME = f"{GRAPH}/me"
_LIST_SELECT = "id,conversationId,subject,from,receivedDateTime,bodyPreview,isRead,hasAttachments"
_TEXT = {"Prefer": 'outlook.body-content-type="text"'}
_LOCAL_TZ = {"Prefer": f'outlook.timezone="{graph_timezone(DEFAULT_TZ)}"'}
_WELL_KNOWN = {"inbox", "sentitems", "drafts", "deleteditems", "junkemail", "archive"}


def _q(value: str) -> str:
    return quote(value, safe="")


def to_kql(query: str) -> str:
    q = re.sub(r"\bafter:(\d{4}-\d{2}-\d{2})", r"received>=\1", query)
    q = re.sub(r"\bbefore:(\d{4}-\d{2}-\d{2})", r"received<=\1", q)
    q = re.sub(r"\bhas:attachment\b", "hasattachments:true", q)
    return q.replace('"', "'")


def _addr(r: dict | None) -> str:
    e = (r or {}).get("emailAddress", {})
    name, address = e.get("name", ""), e.get("address", "")
    return f"{name} <{address}>" if name and name != address else address


def _recips(addresses: list[str] | None) -> list[dict]:
    return [{"emailAddress": {"address": a}} for a in addresses or []]


def _summary(m: dict) -> dict:
    cid = (m.get("conversationId") or "").strip()
    return {
        "message_id": m["id"],
        "thread_id": cid or m["id"],
        "subject": m.get("subject") or "",
        "from": _addr(m.get("from")),
        "date": format_local(m.get("receivedDateTime"), DEFAULT_TZ),
        "snippet": clip(m.get("bodyPreview") or "", 200),
        "unread": not m.get("isRead", True),
        "has_attachments": m.get("hasAttachments", False),
    }


def _merge_msgs(*groups: list[dict]) -> list[dict]:
    out: dict[str, dict] = {}
    for group in groups:
        for row in group:
            mid = row.get("id")
            if mid:
                out[mid] = row
    rows = list(out.values())
    rows.sort(key=lambda m: m.get("receivedDateTime") or "")
    return rows


def _subject_core(subject: str) -> str:
    return re.sub(r"^(re|fw|fwd)\s*:\s*", "", (subject or "").strip(), flags=re.I).strip()


def _looks_like_subject(value: str) -> bool:
    text = (value or "").strip()
    if not text:
        return False
    if " " in text or "<>" in text:
        return True
    return bool(re.match(r"^(re|fw|fwd)\b", text, re.I))


def _looks_like_graph_message_id(value: str) -> bool:
    text = (value or "").strip()
    return text.startswith("AAMk") or (text.startswith("AA") and not text.startswith("AAQk") and " " not in text and len(text) > 20)


def _body_text(m: dict) -> tuple[str, bool]:
    body = m.get("body") or {}
    raw = body.get("content") or ""
    return raw, (body.get("contentType") or "").lower() == "html"


def _event(e: dict) -> dict:
    return {
        "event_id": e.get("id"),
        "title": e.get("subject") or "(no title)",
        "start": format_local((e.get("start") or {}).get("dateTime"), DEFAULT_TZ),
        "end": format_local((e.get("end") or {}).get("dateTime"), DEFAULT_TZ),
        "timezone": tz_label(DEFAULT_TZ),
        "all_day": e.get("isAllDay", False),
        "location": (e.get("location") or {}).get("displayName"),
        "organizer": _addr(e.get("organizer")),
        "attendee_count": len(e.get("attendees") or []),
        "meeting_link": (e.get("onlineMeeting") or {}).get("joinUrl"),
        "show_as": e.get("showAs"),
    }


class OutlookConnector(BaseConnector):
    provider = "outlook"
    tool_specs = MAIL_CALENDAR_TOOLS

    def __init__(self, token_provider: TokenProvider, http: httpx.AsyncClient | None = None, body_limit: int = 2000):
        super().__init__(token_provider)
        self.api = ApiClient("Outlook", token_provider, http)
        self.body_limit = body_limit

    async def _page(self, url: str, params: dict | None, page_token: str | None, headers: dict | None = None):
        if page_token:
            if not page_token.startswith(GRAPH + "/"):
                raise ToolError("Invalid page_token")
            data = await self.api.json("GET", page_token, headers=headers)
        else:
            data = await self.api.json("GET", url, params=params, headers=headers)
        return data.get("value", []), data.get("@odata.nextLink")

    async def _paged(self, url: str, params: dict | None, headers: dict | None, limit: int) -> list[dict]:
        rows, nxt = await self._page(url, params, None, headers=headers)
        out = list(rows)
        while nxt and len(out) < limit:
            rows, nxt = await self._page("", None, nxt, headers=headers)
            out.extend(rows)
        return out[:limit]

    async def _ensure_conversation_ids(self, rows: list[dict]) -> None:
        missing = [m for m in rows if m.get("id") and not m.get("conversationId")]
        if not missing:
            return

        async def fill(m: dict) -> None:
            try:
                data = await self.api.json("GET", f"{ME}/messages/{_q(m['id'])}", params={"$select": "id,conversationId"})
                if data.get("conversationId"):
                    m["conversationId"] = data["conversationId"]
            except Exception:  # noqa: BLE001
                return

        await gather_limited([fill(m) for m in missing], 8)

    async def _get_message_raw(self, message_id: str) -> dict:
        params = {"$select": _LIST_SELECT + ",toRecipients,ccRecipients,body"}
        return await self.api.json("GET", f"{ME}/messages/{_q(message_id)}", params=params, headers=_TEXT)

    async def _resolve_conversation(self, thread_id: str = "", message_id: str = "") -> tuple[str, dict | None]:
        tid = (thread_id or "").strip()
        mid = (message_id or "").strip()
        seed = None
        if mid:
            try:
                seed = await self._get_message_raw(mid)
                cid = (seed.get("conversationId") or "").strip()
                if cid:
                    return cid, seed
            except Exception:  # noqa: BLE001
                pass
        if tid and _looks_like_subject(tid):
            found = await self.tool_search_messages(tid, limit=8)
            hit = (found.get("messages") or [None])[0]
            if isinstance(hit, dict) and hit.get("message_id"):
                try:
                    seed = await self._get_message_raw(hit["message_id"])
                    cid = (seed.get("conversationId") or hit.get("thread_id") or "").strip()
                    if cid:
                        return cid, seed
                except Exception:  # noqa: BLE001
                    cid = (hit.get("thread_id") or "").strip()
                    if cid:
                        return cid, seed
        if tid and (not mid) and (_looks_like_graph_message_id(tid) or not tid.startswith("AAQk")):
            try:
                seed = await self._get_message_raw(tid)
                cid = (seed.get("conversationId") or "").strip()
                if cid:
                    return cid, seed
            except Exception:  # noqa: BLE001
                pass
        if tid:
            return tid, seed
        if seed and seed.get("id"):
            return (seed.get("conversationId") or seed["id"]).strip(), seed
        raise ToolError("thread_id or message_id is required")

    async def _messages_by_conversation(self, cid: str, max_messages: int, seed: dict | None = None) -> list[dict]:
        escaped = cid.replace("'", "''")
        headers = {**_TEXT, "ConsistencyLevel": "eventual"}
        select = _LIST_SELECT + ",toRecipients,ccRecipients,body"
        filt = f"conversationId eq '{escaped}'"
        rows = await self._paged(
            f"{ME}/messages",
            {"$filter": filt, "$count": "true", "$top": min(max(max_messages, 50), 100), "$select": select},
            headers,
            max_messages,
        )
        if len(rows) <= 1:
            try:
                searched = await self._paged(
                    f"{ME}/messages",
                    {"$search": f'"conversationid:{cid}"', "$top": min(max_messages, 50), "$select": select},
                    _TEXT,
                    max_messages,
                )
                rows = _merge_msgs(rows, searched)
            except Exception:  # noqa: BLE001
                pass
        if len(rows) <= 1:
            for fid in ("inbox", "sentitems", "deleteditems", "archive", "junkemail", "drafts"):
                try:
                    extra = await self._paged(
                        f"{ME}/mailFolders/{fid}/messages",
                        {"$filter": filt, "$top": min(max_messages, 50), "$select": select},
                        headers,
                        max_messages,
                    )
                    rows = _merge_msgs(rows, extra)
                except Exception:  # noqa: BLE001
                    continue
                if len({m.get("id") for m in rows}) >= 2:
                    break
        if len(rows) <= 1 and seed:
            core = _subject_core(seed.get("subject") or "")
            if core:
                try:
                    found = await self._paged(
                        f"{ME}/messages",
                        {"$search": f'"{core}"', "$top": min(max_messages, 50), "$select": select},
                        _TEXT,
                        max_messages,
                    )
                    matched = [
                        m
                        for m in found
                        if (m.get("conversationId") or "") == cid or _subject_core(m.get("subject") or "") == core
                    ]
                    rows = _merge_msgs(rows, matched)
                except Exception:  # noqa: BLE001
                    pass
        if seed:
            rows = _merge_msgs(rows, [seed])
        return rows[-max_messages:]

    def _thread_message(self, m: dict, *, body_limit: int, keep_quoted: bool) -> dict:
        raw, is_html = _body_text(m)
        return {
            "message_id": m["id"],
            "thread_id": (m.get("conversationId") or "").strip(),
            "from": _addr(m.get("from")),
            "to": ", ".join(_addr(r) for r in m.get("toRecipients", [])),
            "cc": ", ".join(_addr(r) for r in m.get("ccRecipients", [])),
            "date": format_local(m.get("receivedDateTime"), DEFAULT_TZ),
            "subject": m.get("subject") or "",
            "body": clean_body(raw, is_html, body_limit, keep_quoted=keep_quoted),
            "has_attachments": m.get("hasAttachments", False),
        }

    async def _batch(self, requests: list[dict]) -> tuple[int, int]:
        """Graph $batch, 20 requests per call. Returns (ok, failed)."""
        chunks = [requests[i : i + 20] for i in range(0, len(requests), 20)]

        async def run(chunk):
            body = {
                "requests": [
                    {"id": str(n), "headers": {"Content-Type": "application/json"}, **r} for n, r in enumerate(chunk)
                ]
            }
            data = await self.api.json("POST", f"{GRAPH}/$batch", json=body)
            statuses = [r.get("status", 500) for r in data.get("responses", [])]
            return sum(1 for s in statuses if s < 300), sum(1 for s in statuses if s >= 300)

        results = await gather_limited([run(c) for c in chunks], 4)
        return sum(r[0] for r in results), sum(r[1] for r in results)

    async def _collect_ids(self, query: str, limit: int) -> list[str]:
        folder = folder_from_query(query)
        if folder:
            return await self._collect_folder_ids(folder, limit)
        ids: list[str] = []
        params = {"$search": f'"{to_kql(query)}"', "$top": min(100, limit), "$select": "id"}
        rows, nxt = await self._page(f"{ME}/messages", params, None)
        ids.extend(r["id"] for r in rows)
        while nxt and len(ids) < limit:
            rows, nxt = await self._page("", None, nxt)
            ids.extend(r["id"] for r in rows)
        return ids[:limit]

    def _well_known_folder(self, folder: str) -> str:
        key = canonical_mail_folder(folder)
        return {"spam": "junkemail", "trash": "deleteditems", "drafts": "drafts"}[key]

    async def _collect_folder_ids(self, folder: str, limit: int) -> list[str]:
        try:
            fid = self._well_known_folder(folder)
        except ToolError:
            fid = folder.lower() if folder.lower() in _WELL_KNOWN else _q(folder)
        ids: list[str] = []
        params = {"$select": "id", "$top": min(100, max(limit, 1))}
        rows, nxt = await self._page(f"{ME}/mailFolders/{fid}/messages", params, None)
        ids.extend(r["id"] for r in rows)
        while nxt and len(ids) < limit:
            rows, nxt = await self._page("", None, nxt)
            ids.extend(r["id"] for r in rows)
        return ids[:limit]

    # ---- mail read -------------------------------------------------------

    async def tool_list_messages(self, folder: str = "inbox", unread_only: bool = False, limit: int = 15, page_token: str | None = None):
        fid = folder.lower() if folder.lower() in _WELL_KNOWN else _q(folder)
        params: dict[str, Any] = {"$top": min(limit, 50), "$select": _LIST_SELECT, "$orderby": "receivedDateTime desc"}
        if unread_only:
            params["$filter"] = "isRead eq false"
        rows, nxt = await self._page(f"{ME}/mailFolders/{fid}/messages", params, page_token)
        await self._ensure_conversation_ids(rows)
        return {"messages": [_summary(m) for m in rows], "next_page_token": nxt}

    async def tool_search_messages(self, query: str, limit: int = 15, page_token: str | None = None):
        params = {"$search": f'"{to_kql(query)}"', "$top": min(limit, 50), "$select": _LIST_SELECT}
        rows, nxt = await self._page(f"{ME}/messages", params, page_token)
        await self._ensure_conversation_ids(rows)
        return {"messages": [_summary(m) for m in rows], "next_page_token": nxt}

    async def tool_get_message(self, message_id: str):
        m = await self._get_message_raw(message_id)
        atts = (await self.tool_list_attachments(message_id))["attachments"] if m.get("hasAttachments") else []
        raw, is_html = _body_text(m)
        return {
            **_summary(m),
            "to": ", ".join(_addr(r) for r in m.get("toRecipients", [])),
            "cc": ", ".join(_addr(r) for r in m.get("ccRecipients", [])),
            "body": clean_body(raw, is_html, self.body_limit),
            "attachments": atts,
        }

    async def tool_get_thread(self, thread_id: str = "", max_messages: int = 50, message_id: str = "", full: bool = False):
        cid, seed = await self._resolve_conversation(thread_id, message_id)
        rows = await self._messages_by_conversation(cid, max(1, min(max_messages, 200)), seed)
        keep_quoted = full or len(rows) <= 1
        body_limit = 20000 if full else max(self.body_limit, 2500)
        messages = [self._thread_message(m, body_limit=body_limit, keep_quoted=keep_quoted) for m in rows]
        return {
            "thread_id": cid,
            "subject": rows[0].get("subject", "") if rows else "",
            "message_count": len(messages),
            "last_message_id": rows[-1]["id"] if rows else None,
            "messages": messages,
        }

    async def tool_list_folders(self):
        params = {"$top": 100, "$select": "id,displayName,unreadItemCount,totalItemCount"}
        data = await self.api.json("GET", f"{ME}/mailFolders", params=params)
        return {
            "folders": [
                {"id": f["id"], "name": f["displayName"], "unread": f.get("unreadItemCount"), "total": f.get("totalItemCount")}
                for f in data.get("value", [])
            ]
        }

    # ---- mail manage -----------------------------------------------------

    async def tool_mark_read(self, message_ids: list[str], read: bool = True):
        ok, failed = await self._batch(
            [{"method": "PATCH", "url": f"/me/messages/{_q(i)}", "body": {"isRead": read}} for i in message_ids]
        )
        return {"updated": ok, "failed": failed, "read": read}

    async def tool_archive_messages(self, message_ids: list[str]):
        ok, failed = await self._batch(
            [{"method": "POST", "url": f"/me/messages/{_q(i)}/move", "body": {"destinationId": "archive"}} for i in message_ids]
        )
        return {"archived": ok, "failed": failed}

    async def tool_bulk_delete(
        self,
        message_ids: list[str] | None = None,
        query: str | None = None,
        permanent: bool = False,
        limit: int = 500,
        dry_run: bool = True,
    ):
        limit = max(1, min(limit, 1000))
        if message_ids:
            ids = list(dict.fromkeys(message_ids))[:limit]
        elif query:
            ids = await self._collect_ids(query, limit)
        else:
            raise ToolError("Provide message_ids or query")
        if dry_run:
            sample = await gather_limited(
                [
                    self.api.json("GET", f"{ME}/messages/{_q(i)}", params={"$select": _LIST_SELECT})
                    for i in ids[:5]
                ]
            )
            return {"count": len(ids), "permanent": permanent, "sample": [_summary(m) for m in sample], "message_ids": ids}
        if permanent:
            reqs = [{"method": "POST", "url": f"/me/messages/{_q(i)}/permanentDelete", "body": {}} for i in ids]
        else:
            reqs = [
                {"method": "POST", "url": f"/me/messages/{_q(i)}/move", "body": {"destinationId": "deleteditems"}}
                for i in ids
            ]
        ok, failed = await self._batch(reqs)
        return {"deleted": ok, "failed": failed, "permanent": permanent, "message_ids": ids}

    async def tool_empty_folder(self, folder: str, dry_run: bool = True, limit: int = 1000):
        key = canonical_mail_folder(folder)
        limit = max(1, min(limit, 1000))
        ids = await self._collect_folder_ids(key, limit)
        if dry_run:
            sample = await gather_limited(
                [self.api.json("GET", f"{ME}/messages/{_q(i)}", params={"$select": _LIST_SELECT}) for i in ids[:5]]
            )
            return {"folder": key, "count": len(ids), "sample": [_summary(m) for m in sample], "message_ids": ids}
        if key == "drafts":
            reqs = [{"method": "DELETE", "url": f"/me/messages/{_q(i)}"} for i in ids]
        else:
            reqs = [{"method": "POST", "url": f"/me/messages/{_q(i)}/permanentDelete", "body": {}} for i in ids]
        ok, failed = await self._batch(reqs)
        if failed and key != "drafts":
            moved, move_failed = await self._batch(
                [{"method": "POST", "url": f"/me/messages/{_q(i)}/move", "body": {"destinationId": "deleteditems"}} for i in ids]
            )
            return {
                "folder": key,
                "deleted": max(ok, moved),
                "failed": min(failed, move_failed),
                "permanent": bool(ok and not failed),
                "message_ids": ids,
            }
        return {"folder": key, "deleted": ok, "failed": failed, "permanent": True, "message_ids": ids}

    # ---- mail write ------------------------------------------------------

    @staticmethod
    def _message(to, subject, body, cc=None, bcc=None) -> dict:
        msg = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": _recips(to),
        }
        if cc:
            msg["ccRecipients"] = _recips(cc)
        if bcc:
            msg["bccRecipients"] = _recips(bcc)
        return msg

    def _file_attachments(self, files: list[dict] | None) -> list[dict]:
        out = []
        for item in files or []:
            data = item.get("data") or b""
            if not data:
                continue
            out.append(
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": item.get("filename") or "file",
                    "contentType": item.get("content_type") or "application/octet-stream",
                    "contentBytes": base64.b64encode(data).decode(),
                }
            )
        return out

    async def tool_create_draft(self, to: list[str], subject: str, body: str, cc: list[str] | None = None, files: list | None = None):
        payload = self._message(to, subject, body, cc)
        atts = self._file_attachments(files)
        if atts:
            payload["attachments"] = atts
        data = await self.api.json("POST", f"{ME}/messages", json=payload)
        return {"draft_id": data.get("id"), "message_id": data.get("id")}

    async def tool_send_message(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        files: list | None = None,
    ):
        payload = self._message(to, subject, body, cc, bcc)
        atts = self._file_attachments(files)
        if atts:
            payload["attachments"] = atts
        await self.api.json("POST", f"{ME}/sendMail", json={"message": payload, "saveToSentItems": True})
        return {"sent": True}

    async def tool_reply(self, message_id: str, body: str, reply_all: bool = False, files: list | None = None):
        atts = self._file_attachments(files)
        if not atts:
            action = "replyAll" if reply_all else "reply"
            await self.api.json("POST", f"{ME}/messages/{_q(message_id)}/{action}", json={"comment": body})
            return {"sent": True, "reply_all": reply_all}
        path = "createReplyAll" if reply_all else "createReply"
        draft = await self.api.json("POST", f"{ME}/messages/{_q(message_id)}/{path}")
        did = draft.get("id")
        await self.api.json("PATCH", f"{ME}/messages/{_q(did)}", json={"body": {"contentType": "Text", "content": body}})
        for att in atts:
            await self.api.json("POST", f"{ME}/messages/{_q(did)}/attachments", json=att)
        await self.api.json("POST", f"{ME}/messages/{_q(did)}/send")
        return {"sent": True, "reply_all": reply_all, "attachments": len(atts)}

    async def tool_forward(self, message_id: str, to: list[str], comment: str = ""):
        await self.api.json(
            "POST", f"{ME}/messages/{_q(message_id)}/forward", json={"comment": comment, "toRecipients": _recips(to)}
        )
        return {"sent": True}

    async def _thread_ids(self, thread_id: str) -> list[str]:
        cid, seed = await self._resolve_conversation(thread_id, "")
        rows = await self._messages_by_conversation(cid, 200, seed)
        return [r["id"] for r in rows if r.get("id")]

    async def tool_delete_message(self, message_id: str, permanent: bool = False):
        if permanent:
            await self.api.json("POST", f"{ME}/messages/{_q(message_id)}/permanentDelete")
        else:
            await self.api.json(
                "POST", f"{ME}/messages/{_q(message_id)}/move", json={"destinationId": "deleteditems"}
            )
        return {"deleted": True, "message_id": message_id, "permanent": permanent}

    async def tool_list_threads(self, query: str | None = None, limit: int = 15, page_token: str | None = None):
        params: dict[str, Any] = {
            "$top": min(max(limit * 3, 15), 50),
            "$select": _LIST_SELECT,
            "$orderby": "receivedDateTime desc",
        }
        if query:
            params = {"$search": f'"{to_kql(query)}"', "$top": min(max(limit * 3, 15), 50), "$select": _LIST_SELECT}
        rows, nxt = await self._page(f"{ME}/messages", params, page_token)
        await self._ensure_conversation_ids(rows)
        seen: dict[str, dict] = {}
        for m in rows:
            cid = m.get("conversationId") or m["id"]
            if cid not in seen:
                seen[cid] = {
                    "thread_id": cid,
                    "subject": m.get("subject") or "",
                    "snippet": clip(m.get("bodyPreview") or "", 200),
                    "date": format_local(m.get("receivedDateTime"), DEFAULT_TZ),
                    "from": _addr(m.get("from")),
                }
            if len(seen) >= limit:
                break
        return {"threads": list(seen.values()), "next_page_token": nxt}

    async def tool_trash_thread(self, thread_id: str):
        ids = await self._thread_ids(thread_id)
        ok, failed = await self._batch(
            [{"method": "POST", "url": f"/me/messages/{_q(i)}/move", "body": {"destinationId": "deleteditems"}} for i in ids]
        )
        return {"trashed": True, "thread_id": thread_id, "updated": ok, "failed": failed}

    async def tool_delete_thread(self, thread_id: str):
        ids = await self._thread_ids(thread_id)
        ok, failed = await self._batch(
            [{"method": "POST", "url": f"/me/messages/{_q(i)}/permanentDelete", "body": {}} for i in ids]
        )
        return {"deleted": True, "thread_id": thread_id, "permanent": True, "updated": ok, "failed": failed}

    def _is_folder(self, label_id: str) -> bool:
        return label_id.lower() in _WELL_KNOWN or len(label_id) > 20

    async def _apply_labels(self, message_id: str, add_label_ids: list[str] | None, remove_label_ids: list[str] | None):
        add = list(add_label_ids or [])
        remove = list(remove_label_ids or [])
        folders = [x for x in add if self._is_folder(x)]
        cats_add = [x for x in add if not self._is_folder(x)]
        cats_remove = [x for x in remove if not self._is_folder(x)]
        if any(x.lower() == "inbox" for x in remove) or any(x.lower() == "archive" for x in add):
            await self.api.json("POST", f"{ME}/messages/{_q(message_id)}/move", json={"destinationId": "archive"})
        elif folders:
            await self.api.json("POST", f"{ME}/messages/{_q(message_id)}/move", json={"destinationId": folders[0]})
        if cats_add or cats_remove:
            msg = await self.api.json("GET", f"{ME}/messages/{_q(message_id)}", params={"$select": "categories"})
            cats = [c for c in (msg.get("categories") or []) if c not in cats_remove]
            for c in cats_add:
                if c not in cats:
                    cats.append(c)
            await self.api.json("PATCH", f"{ME}/messages/{_q(message_id)}", json={"categories": cats})
        if "UNREAD" in [x.upper() for x in remove]:
            await self.api.json("PATCH", f"{ME}/messages/{_q(message_id)}", json={"isRead": True})
        if "UNREAD" in [x.upper() for x in add]:
            await self.api.json("PATCH", f"{ME}/messages/{_q(message_id)}", json={"isRead": False})

    async def tool_modify_thread_labels(
        self, thread_id: str, add_label_ids: list[str] | None = None, remove_label_ids: list[str] | None = None
    ):
        ids = await self._thread_ids(thread_id)
        for mid in ids:
            await self._apply_labels(mid, add_label_ids, remove_label_ids)
        return {"updated": True, "thread_id": thread_id, "messages": len(ids)}

    async def tool_list_drafts(self, limit: int = 15, page_token: str | None = None):
        params = {"$top": min(limit, 50), "$select": _LIST_SELECT + ",toRecipients", "$orderby": "receivedDateTime desc"}
        rows, nxt = await self._page(f"{ME}/mailFolders/drafts/messages", params, page_token)
        drafts = []
        for m in rows:
            drafts.append(
                {
                    "draft_id": m["id"],
                    "message_id": m["id"],
                    "subject": m.get("subject") or "",
                    "to": ", ".join(_addr(r) for r in m.get("toRecipients") or []),
                    "snippet": clip(m.get("bodyPreview") or "", 200),
                    "date": format_local(m.get("receivedDateTime"), DEFAULT_TZ),
                }
            )
        return {"drafts": drafts, "next_page_token": nxt}

    async def tool_get_draft(self, draft_id: str):
        data = await self.tool_get_message(draft_id)
        return {"draft_id": draft_id, **data}

    async def tool_send_draft(self, draft_id: str):
        await self.api.json("POST", f"{ME}/messages/{_q(draft_id)}/send")
        return {"sent": True, "draft_id": draft_id}

    async def tool_delete_draft(self, draft_id: str):
        await self.api.json("DELETE", f"{ME}/messages/{_q(draft_id)}")
        return {"deleted": True, "draft_id": draft_id}

    async def tool_modify_labels(
        self, message_id: str, add_label_ids: list[str] | None = None, remove_label_ids: list[str] | None = None
    ):
        await self._apply_labels(message_id, add_label_ids, remove_label_ids)
        return {"updated": True, "message_id": message_id}

    async def tool_batch_modify_messages(
        self, message_ids: list[str], add_label_ids: list[str] | None = None, remove_label_ids: list[str] | None = None
    ):
        for mid in message_ids:
            await self._apply_labels(mid, add_label_ids, remove_label_ids)
        return {"updated": len(message_ids)}

    async def tool_create_label(self, name: str):
        data = await self.api.json("POST", f"{ME}/mailFolders", json={"displayName": name})
        return {"label_id": data.get("id"), "name": data.get("displayName")}

    async def tool_get_label(self, label_id: str):
        fid = label_id.lower() if label_id.lower() in _WELL_KNOWN else _q(label_id)
        data = await self.api.json(
            "GET", f"{ME}/mailFolders/{fid}", params={"$select": "id,displayName,unreadItemCount,totalItemCount"}
        )
        return {
            "label_id": data.get("id"),
            "name": data.get("displayName"),
            "type": "system" if label_id.lower() in _WELL_KNOWN else "user",
            "messages_total": data.get("totalItemCount"),
            "messages_unread": data.get("unreadItemCount"),
        }

    async def tool_patch_label(self, label_id: str, name: str):
        data = await self.api.json("PATCH", f"{ME}/mailFolders/{_q(label_id)}", json={"displayName": name})
        return {"label_id": data.get("id"), "name": data.get("displayName")}

    async def tool_delete_label(self, label_id: str):
        await self.api.json("DELETE", f"{ME}/mailFolders/{_q(label_id)}")
        return {"deleted": True, "label_id": label_id}

    def _rule(self, row: dict) -> dict:
        return {
            "filter_id": row.get("id"),
            "name": row.get("displayName"),
            "enabled": row.get("isEnabled"),
            "conditions": row.get("conditions") or {},
            "actions": row.get("actions") or {},
        }

    async def tool_list_filters(self):
        data = await self.api.json("GET", f"{ME}/mailFolders/inbox/messageRules")
        return {"filters": [self._rule(r) for r in data.get("value") or []]}

    async def tool_get_filter(self, filter_id: str):
        data = await self.api.json("GET", f"{ME}/mailFolders/inbox/messageRules/{_q(filter_id)}")
        return self._rule(data)

    async def tool_create_filter(
        self,
        name: str | None = None,
        from_email: str | None = None,
        subject: str | None = None,
        query: str | None = None,
        add_label_ids: list[str] | None = None,
        skip_inbox: bool = False,
        trash: bool = False,
    ):
        conditions: dict[str, Any] = {}
        if from_email:
            conditions["fromAddresses"] = [{"emailAddress": {"address": from_email}}]
        if subject:
            conditions["subjectContains"] = [subject]
        if query:
            conditions["bodyOrSubjectContains"] = [query]
        if not conditions:
            raise ToolError("Provide from_email, subject, or query")
        actions: dict[str, Any] = {}
        dest = None
        if trash:
            dest = "deleteditems"
        elif add_label_ids:
            dest = add_label_ids[0]
        elif skip_inbox:
            dest = "archive"
        if dest:
            actions["moveToFolder"] = dest
        if skip_inbox and not dest:
            actions["moveToFolder"] = "archive"
        raw = await self.api.json("GET", f"{ME}/mailFolders/inbox/messageRules")
        sequence = max((int(r.get("sequence") or 0) for r in raw.get("value") or []), default=0) + 1
        data = await self.api.json(
            "POST",
            f"{ME}/mailFolders/inbox/messageRules",
            json={
                "displayName": name or "Assistant filter",
                "sequence": sequence,
                "isEnabled": True,
                "conditions": conditions,
                "actions": actions or {"markAsRead": False},
            },
        )
        return self._rule(data)

    async def tool_delete_filter(self, filter_id: str):
        await self.api.json("DELETE", f"{ME}/mailFolders/inbox/messageRules/{_q(filter_id)}")
        return {"deleted": True, "filter_id": filter_id}

    async def tool_get_profile(self):
        me = await self.api.json("GET", f"{ME}?$select=mail,userPrincipalName,displayName")
        return {
            "email": me.get("mail") or me.get("userPrincipalName"),
            "name": me.get("displayName"),
        }

    async def tool_get_vacation_settings(self):
        data = await self.api.json("GET", f"{ME}/mailboxSettings/automaticRepliesSetting")
        return {
            "enabled": data.get("status") not in (None, "disabled"),
            "status": data.get("status"),
            "body": (data.get("internalReplyMessage") or data.get("externalReplyMessage") or ""),
            "start": (data.get("scheduledStartDateTime") or {}).get("dateTime"),
            "end": (data.get("scheduledEndDateTime") or {}).get("dateTime"),
        }

    async def tool_get_auto_forwarding(self):
        rules = (await self.tool_list_filters()).get("filters") or []
        for rule in rules:
            actions = rule.get("actions") or {}
            dest = (actions.get("forwardTo") or actions.get("redirectTo") or [None])[0]
            if dest or actions.get("forwardTo") or actions.get("redirectTo"):
                addrs = actions.get("forwardTo") or actions.get("redirectTo") or []
                email = ""
                if addrs and isinstance(addrs[0], dict):
                    email = ((addrs[0].get("emailAddress") or {}).get("address")) or ""
                return {"enabled": bool(rule.get("enabled")), "email": email, "disposition": "rule", "filter_id": rule.get("filter_id")}
        return {"enabled": False}

    async def tool_list_forwarding_addresses(self):
        rules = (await self.tool_list_filters()).get("filters") or []
        addresses = []
        for rule in rules:
            actions = rule.get("actions") or {}
            for key in ("forwardTo", "redirectTo"):
                for item in actions.get(key) or []:
                    email = ((item or {}).get("emailAddress") or {}).get("address")
                    if email:
                        addresses.append({"email": email, "status": "rule"})
        return {"addresses": addresses}

    async def tool_list_send_as(self):
        me = await self.api.json("GET", f"{ME}?$select=mail,userPrincipalName,otherMails")
        primary = me.get("mail") or me.get("userPrincipalName")
        aliases = [{"email": primary, "name": "", "primary": True, "default": True}]
        for extra in me.get("otherMails") or []:
            if extra and extra != primary:
                aliases.append({"email": extra, "name": "", "primary": False, "default": False})
        return {"aliases": aliases}

    async def tool_get_language_settings(self):
        data = await self.api.json("GET", f"{ME}/mailboxSettings")
        lang = data.get("language") or {}
        return {"language": lang.get("locale") or lang.get("displayName")}

    async def tool_get_imap_settings(self):
        return {"supported": False, "provider": "outlook", "note": "Outlook on Microsoft 365 does not expose IMAP settings via Graph."}

    async def tool_get_pop_settings(self):
        return {"supported": False, "provider": "outlook", "note": "Outlook on Microsoft 365 does not expose POP settings via Graph."}

    def _outlook_contact(self, row: dict) -> dict:
        emails = [e.get("address") for e in (row.get("emailAddresses") or []) if e.get("address")]
        phones = [p for p in [row.get("mobilePhone"), *((row.get("businessPhones") or []))] if p]
        return {
            "contact_id": row.get("id"),
            "name": row.get("displayName") or "",
            "emails": emails,
            "phones": phones,
            "organization": row.get("companyName") or "",
        }

    async def tool_get_contacts(self, limit: int = 15, page_token: str | None = None):
        params = {"$top": min(limit, 50), "$select": "id,displayName,emailAddresses,mobilePhone,businessPhones,companyName"}
        rows, nxt = await self._page(f"{ME}/contacts", params, page_token)
        return {"contacts": [self._outlook_contact(r) for r in rows], "next_page_token": nxt}

    async def tool_search_people(self, query: str, limit: int = 15):
        params = {"$search": f'"{query}"', "$top": min(limit, 30)}
        try:
            rows, _ = await self._page(f"{ME}/people", params, None)
            contacts = []
            for row in rows:
                emails = [((s.get("emailAddress") or {}).get("address")) for s in (row.get("scoredEmailAddresses") or [])]
                contacts.append(
                    {
                        "contact_id": row.get("id"),
                        "name": row.get("displayName") or "",
                        "emails": [e for e in emails if e],
                        "phones": [],
                        "organization": (row.get("companyName") or ""),
                    }
                )
            if contacts:
                return {"contacts": contacts}
        except Exception:  # noqa: BLE001
            pass
        params = {
            "$filter": f"contains(displayName,'{query.replace(chr(39), '')}')",
            "$top": min(limit, 30),
            "$select": "id,displayName,emailAddresses,mobilePhone,businessPhones,companyName",
        }
        rows, _ = await self._page(f"{ME}/contacts", params, None)
        return {"contacts": [self._outlook_contact(r) for r in rows]}

    async def tool_get_people(self, contact_id: str | None = None, other_contacts: bool = False):
        if other_contacts:
            rows, _ = await self._page(
                f"{ME}/contacts",
                {"$top": 20, "$select": "id,displayName,emailAddresses,mobilePhone,businessPhones,companyName"},
                None,
            )
            return {"contacts": [self._outlook_contact(r) for r in rows]}
        if not contact_id:
            raise ToolError("Provide contact_id or set other_contacts=true")
        row = await self.api.json("GET", f"{ME}/contacts/{_q(contact_id)}")
        return self._outlook_contact(row)

    # ---- attachments -----------------------------------------------------

    async def tool_list_attachments(self, message_id: str):
        params = {"$select": "id,name,contentType,size,isInline"}
        data = await self.api.json("GET", f"{ME}/messages/{_q(message_id)}/attachments", params=params)
        return {
            "message_id": message_id,
            "attachments": [
                {"attachment_id": a["id"], "filename": a.get("name"), "mime_type": a.get("contentType"), "size": a.get("size", 0)}
                for a in data.get("value", [])
                if not a.get("isInline")
            ],
        }

    async def tool_download_attachment(self, message_id: str, attachment_id: str):
        a = await self.api.json("GET", f"{ME}/messages/{_q(message_id)}/attachments/{_q(attachment_id)}")
        if "contentBytes" not in a:
            raise ToolError("This attachment is an embedded item, not a file, and cannot be downloaded")
        return BinaryResult(
            filename=a.get("name") or "attachment",
            content_type=a.get("contentType") or "application/octet-stream",
            data=base64.b64decode(a["contentBytes"]),
        )

    # ---- calendar --------------------------------------------------------

    async def _calendar_view(self, start: datetime, end: datetime, limit: int = 100) -> list[dict]:
        params = {
            "startDateTime": as_utc(start, DEFAULT_TZ).isoformat(),
            "endDateTime": as_utc(end, DEFAULT_TZ).isoformat(),
            "$top": min(limit, 250),
            "$orderby": "start/dateTime",
            "$select": "id,subject,start,end,location,organizer,attendees,onlineMeeting,isAllDay,showAs,bodyPreview",
        }
        rows, _ = await self._page(f"{ME}/calendarView", params, None, headers=_LOCAL_TZ)
        return rows

    async def tool_list_events(self, start: str, end: str, limit: int = 25):
        rows = await self._calendar_view(parse_iso(start, DEFAULT_TZ), parse_iso(end, DEFAULT_TZ), limit)
        return {"events": [_event(e) for e in rows[:limit]]}

    async def tool_get_event(self, event_id: str):
        e = await self.api.json("GET", f"{ME}/events/{_q(event_id)}", headers={**_LOCAL_TZ, **_TEXT})
        return {
            **_event(e),
            "description": clean_body((e.get("body") or {}).get("content", ""), False, 1500),
            "attendees": [
                {"email": (a.get("emailAddress") or {}).get("address"), "response": (a.get("status") or {}).get("response")}
                for a in e.get("attendees") or []
            ],
        }

    async def tool_search_events(self, query: str, start: str | None = None, end: str | None = None):
        now = now_local(DEFAULT_TZ)
        rows = await self._calendar_view(
            parse_iso(start, DEFAULT_TZ) if start else now,
            parse_iso(end, DEFAULT_TZ) if end else now + timedelta(days=30),
            250,
        )
        terms = [t for t in query.lower().split() if t]

        def hit(e: dict) -> bool:
            hay = " ".join(
                [e.get("subject") or "", (e.get("location") or {}).get("displayName") or "", _addr(e.get("organizer")), e.get("bodyPreview") or ""]
            ).lower()
            return all(t in hay for t in terms)

        return {"events": [_event(e) for e in rows if hit(e)][:50]}

    async def tool_find_free_slots(
        self,
        start: str,
        end: str,
        duration_minutes: int = 30,
        work_hours_start: str = "09:00",
        work_hours_end: str = "18:00",
        timezone: str = DEFAULT_TZ,
    ):
        s, e = parse_iso(start, timezone), parse_iso(end, timezone)
        rows = await self._calendar_view(s, e, 250)
        busy = [
            (parse_iso(r["start"]["dateTime"], timezone), parse_iso(r["end"]["dateTime"], timezone))
            for r in rows
            if r.get("showAs") not in ("free", "workingElsewhere")
        ]
        return {"slots": free_slots(busy, s, e, duration_minutes, work_hours_start, work_hours_end, timezone)}

    async def tool_create_event(
        self,
        title: str,
        start: str,
        end: str | None = None,
        duration_minutes: int = 30,
        attendees: list[str] | None = None,
        description: str = "",
        location: str = "",
        timezone: str = DEFAULT_TZ,
        send_invites: bool = True,
        conference: str = "none",
    ):
        zone = timezone or DEFAULT_TZ
        start_dt = as_local(start, zone)
        end_dt = as_local(end, zone) if end else start_dt + timedelta(minutes=duration_minutes or 30)
        kind = (conference or "none").lower()
        note = description or ""
        payload: dict[str, Any] = {
            "subject": title,
            "body": {"contentType": "text", "content": note},
            "start": {"dateTime": graph_datetime(start_dt, zone), "timeZone": graph_timezone(zone)},
            "end": {"dateTime": graph_datetime(end_dt, zone), "timeZone": graph_timezone(zone)},
            "attendees": [{"emailAddress": {"address": a}, "type": "required"} for a in (attendees or []) if a],
        }
        if location:
            payload["location"] = {"displayName": location}
        if kind in {"teams", "team"}:
            payload["isOnlineMeeting"] = True
            payload["onlineMeetingProvider"] = "teamsForBusiness"
        elif kind in {"google_meet", "meet", "hangouts"}:
            payload["location"] = {"displayName": location or "Google Meet"}
            payload["body"] = {
                "contentType": "text",
                "content": (note + "\n\n" if note else "")
                + "Google Meet links are created from Gmail. This Outlook event has no Meet URL.",
            }
        e = await self.api.json("POST", f"{ME}/events", json=payload)
        if kind in {"teams", "team"} and not (e.get("onlineMeeting") or {}).get("joinUrl"):
            e = await self.api.json(
                "GET",
                f"{ME}/events/{_q(e.get('id') or '')}",
                params={"$select": "id,subject,start,end,location,organizer,attendees,onlineMeeting,isAllDay,showAs"},
                headers=_LOCAL_TZ,
            )
        out = {**_event(e), "invites_sent": bool(send_invites and attendees), "timezone": tz_label(zone), "conference": kind}
        if kind in {"google_meet", "meet", "hangouts"}:
            out["hint"] = "Use Gmail create_google_meet for a Google Meet join URL."
        return out

    async def tool_create_google_meet(self, title: str, start: str, attendees: list[str], end: str | None = None, duration_minutes: int = 30, description: str = "", timezone: str = DEFAULT_TZ):
        return await self.tool_create_event(
            title=title,
            start=start,
            end=end,
            duration_minutes=duration_minutes,
            attendees=attendees,
            description=description,
            timezone=timezone,
            conference="google_meet",
        )

    async def tool_create_teams_meeting(self, title: str, start: str, attendees: list[str], end: str | None = None, duration_minutes: int = 30, description: str = "", timezone: str = DEFAULT_TZ):
        return await self.tool_create_event(
            title=title,
            start=start,
            end=end,
            duration_minutes=duration_minutes,
            attendees=attendees,
            description=description,
            timezone=timezone,
            conference="teams",
        )
