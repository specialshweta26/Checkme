from __future__ import annotations

import base64
import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from connector_core import MAIL_CALENDAR_TOOLS, BaseConnector, BinaryResult, ToolError, TokenProvider
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
    return {
        "message_id": m["id"],
        "thread_id": m.get("conversationId"),
        "subject": m.get("subject") or "",
        "from": _addr(m.get("from")),
        "date": format_local(m.get("receivedDateTime"), DEFAULT_TZ),
        "snippet": clip(m.get("bodyPreview") or "", 200),
        "unread": not m.get("isRead", True),
        "has_attachments": m.get("hasAttachments", False),
    }


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
        ids: list[str] = []
        params = {"$search": f'"{to_kql(query)}"', "$top": min(100, limit), "$select": "id"}
        rows, nxt = await self._page(f"{ME}/messages", params, None)
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
        return {"messages": [_summary(m) for m in rows], "next_page_token": nxt}

    async def tool_search_messages(self, query: str, limit: int = 15, page_token: str | None = None):
        params = {"$search": f'"{to_kql(query)}"', "$top": min(limit, 50), "$select": _LIST_SELECT}
        rows, nxt = await self._page(f"{ME}/messages", params, page_token)
        return {"messages": [_summary(m) for m in rows], "next_page_token": nxt}

    async def tool_get_message(self, message_id: str):
        params = {"$select": _LIST_SELECT + ",toRecipients,ccRecipients,body"}
        m = await self.api.json("GET", f"{ME}/messages/{_q(message_id)}", params=params, headers=_TEXT)
        atts = (await self.tool_list_attachments(message_id))["attachments"] if m.get("hasAttachments") else []
        return {
            **_summary(m),
            "to": ", ".join(_addr(r) for r in m.get("toRecipients", [])),
            "cc": ", ".join(_addr(r) for r in m.get("ccRecipients", [])),
            "body": clean_body((m.get("body") or {}).get("content", ""), False, self.body_limit),
            "attachments": atts,
        }

    async def tool_get_thread(self, thread_id: str, max_messages: int = 50):
        cid = thread_id.replace("'", "''")
        params = {
            "$filter": f"conversationId eq '{cid}'",
            "$top": min(max_messages, 200),
            "$select": _LIST_SELECT + ",toRecipients,body",
        }
        rows, _ = await self._page(f"{ME}/messages", params, None, headers=_TEXT)
        rows.sort(key=lambda m: m.get("receivedDateTime", ""))
        rows = rows[-max_messages:]
        return {
            "thread_id": thread_id,
            "subject": rows[0].get("subject", "") if rows else "",
            "message_count": len(rows),
            "last_message_id": rows[-1]["id"] if rows else None,
            "messages": [
                {
                    "message_id": m["id"],
                    "from": _addr(m.get("from")),
                    "to": ", ".join(_addr(r) for r in m.get("toRecipients", [])),
                    "date": format_local(m.get("receivedDateTime"), DEFAULT_TZ),
                    "body": clean_body((m.get("body") or {}).get("content", ""), False, 1500),
                    "has_attachments": m.get("hasAttachments", False),
                }
                for m in rows
            ],
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
        return {"deleted": ok, "failed": failed, "permanent": permanent}

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

    async def tool_create_draft(self, to: list[str], subject: str, body: str, cc: list[str] | None = None):
        data = await self.api.json("POST", f"{ME}/messages", json=self._message(to, subject, body, cc))
        return {"draft_id": data.get("id"), "message_id": data.get("id")}

    async def tool_send_message(self, to: list[str], subject: str, body: str, cc: list[str] | None = None, bcc: list[str] | None = None):
        await self.api.json(
            "POST", f"{ME}/sendMail", json={"message": self._message(to, subject, body, cc, bcc), "saveToSentItems": True}
        )
        return {"sent": True}

    async def tool_reply(self, message_id: str, body: str, reply_all: bool = False):
        action = "replyAll" if reply_all else "reply"
        await self.api.json("POST", f"{ME}/messages/{_q(message_id)}/{action}", json={"comment": body})
        return {"sent": True, "reply_all": reply_all}

    async def tool_forward(self, message_id: str, to: list[str], comment: str = ""):
        await self.api.json(
            "POST", f"{ME}/messages/{_q(message_id)}/forward", json={"comment": comment, "toRecipients": _recips(to)}
        )
        return {"sent": True}

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
    ):
        zone = timezone or DEFAULT_TZ
        start_dt = as_local(start, zone)
        end_dt = as_local(end, zone) if end else start_dt + timedelta(minutes=duration_minutes or 30)
        payload: dict[str, Any] = {
            "subject": title,
            "body": {"contentType": "text", "content": description or ""},
            "start": {"dateTime": graph_datetime(start_dt, zone), "timeZone": graph_timezone(zone)},
            "end": {"dateTime": graph_datetime(end_dt, zone), "timeZone": graph_timezone(zone)},
            "attendees": [{"emailAddress": {"address": a}, "type": "required"} for a in (attendees or []) if a],
        }
        if location:
            payload["location"] = {"displayName": location}
        e = await self.api.json("POST", f"{ME}/events", json=payload)
        return {**_event(e), "invites_sent": bool(send_invites and attendees), "timezone": tz_label(zone)}
