from __future__ import annotations

import base64
import re
from datetime import timedelta
from email.message import EmailMessage
from email.utils import getaddresses
from typing import Any

import httpx

from connector_core import MAIL_CALENDAR_TOOLS, BaseConnector, BinaryResult, ToolError, TokenProvider
from connector_core.calendar import DEFAULT_TZ, as_local, format_local, free_slots, iana_tz, local_wall, now_local, parse_iso, tz_label
from connector_core.http import ApiClient, gather_limited
from connector_core.text import clean_body, clip

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
CAL = "https://www.googleapis.com/calendar/v3"
_SYSTEM_LABELS = {"inbox", "sent", "draft", "spam", "trash", "starred", "important", "unread"}
_META_HEADERS = ["From", "To", "Cc", "Subject", "Date", "Message-ID", "References", "Reply-To"]


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def to_gmail_query(query: str) -> str:
    """Accept neutral 'after:2026-07-01' style dates; Gmail wants slashes."""
    return re.sub(r"\b(after|before):(\d{4})-(\d{2})-(\d{2})", r"\1:\2/\3/\4", query)


def _headers(msg: dict) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}


def _walk(part: dict):
    yield part
    for child in part.get("parts", []) or []:
        yield from _walk(child)


def _bodies(payload: dict) -> tuple[str, bool]:
    plain, html = None, None
    for part in _walk(payload):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if not data or part.get("filename"):
            continue
        if mime == "text/plain" and plain is None:
            plain = _b64url_decode(data).decode("utf-8", "replace")
        elif mime == "text/html" and html is None:
            html = _b64url_decode(data).decode("utf-8", "replace")
    if plain is not None:
        return plain, False
    return html or "", True


def _attachments(payload: dict) -> list[dict]:
    out = []
    for part in _walk(payload):
        body = part.get("body", {})
        if part.get("filename") and body.get("attachmentId"):
            # Gmail attachment ids change on every fetch; the part id is stable.
            out.append(
                {
                    "attachment_id": part.get("partId"),
                    "filename": part["filename"],
                    "mime_type": part.get("mimeType"),
                    "size": body.get("size", 0),
                }
            )
    return out


def _summary(msg: dict) -> dict:
    h = _headers(msg)
    labels = msg.get("labelIds", [])
    return {
        "message_id": msg["id"],
        "thread_id": msg.get("threadId"),
        "subject": h.get("subject", ""),
        "from": h.get("from", ""),
        "date": format_local(h.get("date", ""), DEFAULT_TZ),
        "snippet": clip(msg.get("snippet", ""), 200),
        "unread": "UNREAD" in labels,
    }


def _event(e: dict) -> dict:
    start, end = e.get("start", {}), e.get("end", {})
    return {
        "event_id": e.get("id"),
        "title": e.get("summary", "(no title)"),
        "start": format_local(start.get("dateTime") or start.get("date"), DEFAULT_TZ),
        "end": format_local(end.get("dateTime") or end.get("date"), DEFAULT_TZ),
        "timezone": tz_label(DEFAULT_TZ),
        "all_day": "date" in start,
        "location": e.get("location"),
        "organizer": (e.get("organizer") or {}).get("email"),
        "attendee_count": len(e.get("attendees", []) or []),
        "meeting_link": e.get("hangoutLink"),
        "status": e.get("status"),
    }


class GmailConnector(BaseConnector):
    provider = "gmail"
    tool_specs = MAIL_CALENDAR_TOOLS

    def __init__(self, token_provider: TokenProvider, http: httpx.AsyncClient | None = None, body_limit: int = 2000):
        super().__init__(token_provider)
        self.api = ApiClient("Gmail", token_provider, http)
        self.body_limit = body_limit
        self._me: str | None = None

    async def _my_email(self) -> str:
        if self._me is None:
            self._me = (await self.api.json("GET", f"{GMAIL}/profile")).get("emailAddress", "")
        return self._me

    async def _list_ids(self, q: str | None, label_ids: list[str] | None, limit: int, page_token: str | None = None):
        params: dict[str, Any] = {"maxResults": min(limit, 500)}
        if q:
            params["q"] = q
        if label_ids:
            params["labelIds"] = label_ids
        if page_token:
            params["pageToken"] = page_token
        data = await self.api.json("GET", f"{GMAIL}/messages", params=params)
        return [m["id"] for m in data.get("messages", [])], data.get("nextPageToken")

    async def _collect_ids(self, q: str | None, limit: int) -> list[str]:
        ids: list[str] = []
        token = None
        while len(ids) < limit:
            batch, token = await self._list_ids(q, None, limit - len(ids), token)
            ids.extend(batch)
            if not token or not batch:
                break
        return ids[:limit]

    async def _meta(self, ids: list[str]) -> list[dict]:
        params = {"format": "metadata", "metadataHeaders": _META_HEADERS}
        msgs = await gather_limited([self.api.json("GET", f"{GMAIL}/messages/{i}", params=params) for i in ids], 10)
        return [_summary(m) for m in msgs]

    async def _full(self, message_id: str) -> dict:
        return await self.api.json("GET", f"{GMAIL}/messages/{message_id}", params={"format": "full"})

    # ---- mail read -------------------------------------------------------

    async def tool_list_messages(self, folder: str = "INBOX", unread_only: bool = False, limit: int = 15, page_token: str | None = None):
        label = folder.upper() if folder.lower() in _SYSTEM_LABELS else folder
        ids, nxt = await self._list_ids("is:unread" if unread_only else None, [label], limit, page_token)
        return {"messages": await self._meta(ids), "next_page_token": nxt}

    async def tool_search_messages(self, query: str, limit: int = 15, page_token: str | None = None):
        ids, nxt = await self._list_ids(to_gmail_query(query), None, limit, page_token)
        return {"messages": await self._meta(ids), "next_page_token": nxt}

    async def tool_get_message(self, message_id: str):
        msg = await self._full(message_id)
        h = _headers(msg)
        body, is_html = _bodies(msg.get("payload", {}))
        return {
            **_summary(msg),
            "to": h.get("to", ""),
            "cc": h.get("cc", ""),
            "body": clean_body(body, is_html, self.body_limit),
            "attachments": _attachments(msg.get("payload", {})),
        }

    async def tool_get_thread(self, thread_id: str, max_messages: int = 50):
        data = await self.api.json("GET", f"{GMAIL}/threads/{thread_id}", params={"format": "full"})
        msgs = data.get("messages", [])[-max_messages:]
        out = []
        for m in msgs:
            h = _headers(m)
            body, is_html = _bodies(m.get("payload", {}))
            out.append(
                {
                    "message_id": m["id"],
                    "from": h.get("from", ""),
                    "to": h.get("to", ""),
                    "date": format_local(h.get("date", ""), DEFAULT_TZ),
                    "body": clean_body(body, is_html, 1500),
                    "attachments": [a["filename"] for a in _attachments(m.get("payload", {}))],
                }
            )
        first = _headers(msgs[0]) if msgs else {}
        return {
            "thread_id": thread_id,
            "subject": first.get("subject", ""),
            "message_count": len(data.get("messages", [])),
            "last_message_id": msgs[-1]["id"] if msgs else None,
            "messages": out,
        }

    async def tool_list_folders(self):
        data = await self.api.json("GET", f"{GMAIL}/labels")
        return {"folders": [{"id": l["id"], "name": l["name"], "type": l.get("type")} for l in data.get("labels", [])]}

    # ---- mail manage -----------------------------------------------------

    async def _batch_modify(self, ids: list[str], add: list[str] | None = None, remove: list[str] | None = None):
        for i in range(0, len(ids), 1000):
            await self.api.json(
                "POST",
                f"{GMAIL}/messages/batchModify",
                json={"ids": ids[i : i + 1000], "addLabelIds": add or [], "removeLabelIds": remove or []},
            )

    async def tool_mark_read(self, message_ids: list[str], read: bool = True):
        await self._batch_modify(message_ids, remove=["UNREAD"] if read else None, add=None if read else ["UNREAD"])
        return {"updated": len(message_ids), "read": read}

    async def tool_archive_messages(self, message_ids: list[str]):
        await self._batch_modify(message_ids, remove=["INBOX"])
        return {"archived": len(message_ids)}

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
            ids = await self._collect_ids(to_gmail_query(query), limit)
        else:
            raise ToolError("Provide message_ids or query")
        if dry_run:
            sample = await self._meta(ids[:5])
            return {"count": len(ids), "permanent": permanent, "sample": sample, "message_ids": ids}
        if permanent:
            for i in range(0, len(ids), 1000):
                await self.api.json("POST", f"{GMAIL}/messages/batchDelete", json={"ids": ids[i : i + 1000]})
        else:
            await self._batch_modify(ids, add=["TRASH"], remove=["INBOX"])
        return {"deleted": len(ids), "permanent": permanent}

    # ---- mail write ------------------------------------------------------

    @staticmethod
    def _raw(msg: EmailMessage) -> str:
        return base64.urlsafe_b64encode(msg.as_bytes()).decode()

    @staticmethod
    def _compose(to, subject, body, cc=None, bcc=None, headers: dict | None = None) -> EmailMessage:
        msg = EmailMessage()
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        if bcc:
            msg["Bcc"] = ", ".join(bcc)
        msg["Subject"] = subject
        for k, v in (headers or {}).items():
            msg[k] = v
        msg.set_content(body)
        return msg

    async def tool_create_draft(self, to: list[str], subject: str, body: str, cc: list[str] | None = None):
        msg = self._compose(to, subject, body, cc)
        data = await self.api.json("POST", f"{GMAIL}/drafts", json={"message": {"raw": self._raw(msg)}})
        return {"draft_id": data.get("id"), "message_id": data.get("message", {}).get("id")}

    async def tool_send_message(self, to: list[str], subject: str, body: str, cc: list[str] | None = None, bcc: list[str] | None = None):
        msg = self._compose(to, subject, body, cc, bcc)
        data = await self.api.json("POST", f"{GMAIL}/messages/send", json={"raw": self._raw(msg)})
        return {"sent": True, "message_id": data.get("id"), "thread_id": data.get("threadId")}

    async def tool_reply(self, message_id: str, body: str, reply_all: bool = False):
        params = {"format": "metadata", "metadataHeaders": _META_HEADERS}
        orig = await self.api.json("GET", f"{GMAIL}/messages/{message_id}", params=params)
        h = _headers(orig)
        me = (await self._my_email()).lower()
        to = [a for _, a in getaddresses([h.get("reply-to") or h.get("from", "")]) if a]
        cc: list[str] = []
        if reply_all:
            others = [a for _, a in getaddresses([h.get("to", ""), h.get("cc", "")]) if a and a.lower() != me]
            cc = [a for a in dict.fromkeys(others) if a not in to]
        subject = h.get("subject", "")
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        mid = h.get("message-id", "")
        refs = (h.get("references", "") + " " + mid).strip()
        msg = self._compose(to, subject, body, cc, headers={"In-Reply-To": mid, "References": refs})
        data = await self.api.json(
            "POST", f"{GMAIL}/messages/send", json={"raw": self._raw(msg), "threadId": orig.get("threadId")}
        )
        return {"sent": True, "message_id": data.get("id"), "to": to, "cc": cc}

    async def tool_forward(self, message_id: str, to: list[str], comment: str = ""):
        orig = await self._full(message_id)
        h = _headers(orig)
        body, is_html = _bodies(orig.get("payload", {}))
        quoted = clean_body(body, is_html, limit=50_000, keep_quoted=True)
        text = (
            f"{comment}\n\n---------- Forwarded message ---------\n"
            f"From: {h.get('from', '')}\nDate: {format_local(h.get('date', ''), DEFAULT_TZ)}\nSubject: {h.get('subject', '')}\n"
            f"To: {h.get('to', '')}\n\n{quoted}"
        )
        subject = h.get("subject", "")
        msg = self._compose(to, subject if subject.lower().startswith("fwd:") else f"Fwd: {subject}", text)
        for att in _attachments(orig.get("payload", {})):
            blob = await self.tool_download_attachment(message_id, att["attachment_id"], _message=orig)
            maintype, _, subtype = (blob.content_type or "application/octet-stream").partition("/")
            msg.add_attachment(blob.data, maintype=maintype, subtype=subtype or "octet-stream", filename=blob.filename)
        data = await self.api.json("POST", f"{GMAIL}/messages/send", json={"raw": self._raw(msg)})
        return {"sent": True, "message_id": data.get("id")}

    # ---- attachments -----------------------------------------------------

    async def tool_list_attachments(self, message_id: str):
        msg = await self._full(message_id)
        return {"message_id": message_id, "attachments": _attachments(msg.get("payload", {}))}

    async def tool_download_attachment(self, message_id: str, attachment_id: str, _message: dict | None = None):
        msg = _message or await self._full(message_id)
        part = next((p for p in _walk(msg.get("payload", {})) if p.get("partId") == attachment_id), None)
        if not part or not part.get("body", {}).get("attachmentId"):
            raise ToolError("Attachment not found; call list_attachments for valid ids")
        data = await self.api.json(
            "GET", f"{GMAIL}/messages/{message_id}/attachments/{part['body']['attachmentId']}"
        )
        return BinaryResult(
            filename=part.get("filename") or "attachment",
            content_type=part.get("mimeType") or "application/octet-stream",
            data=_b64url_decode(data.get("data", "")),
        )

    # ---- calendar --------------------------------------------------------

    async def tool_list_events(self, start: str, end: str, limit: int = 25):
        params = {
            "timeMin": parse_iso(start, DEFAULT_TZ).isoformat(),
            "timeMax": parse_iso(end, DEFAULT_TZ).isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": min(limit, 100),
            "timeZone": DEFAULT_TZ,
        }
        data = await self.api.json("GET", f"{CAL}/calendars/primary/events", params=params)
        return {"events": [_event(e) for e in data.get("items", [])]}

    async def tool_get_event(self, event_id: str):
        e = await self.api.json("GET", f"{CAL}/calendars/primary/events/{event_id}")
        return {
            **_event(e),
            "description": clean_body(e.get("description", ""), True, 1500),
            "attendees": [
                {"email": a.get("email"), "response": a.get("responseStatus")} for a in e.get("attendees", []) or []
            ],
        }

    async def tool_search_events(self, query: str, start: str | None = None, end: str | None = None):
        now = now_local(DEFAULT_TZ)
        params = {
            "q": query,
            "timeMin": (parse_iso(start, DEFAULT_TZ) if start else now).isoformat(),
            "timeMax": (parse_iso(end, DEFAULT_TZ) if end else now + timedelta(days=30)).isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 50,
            "timeZone": DEFAULT_TZ,
        }
        data = await self.api.json("GET", f"{CAL}/calendars/primary/events", params=params)
        return {"events": [_event(e) for e in data.get("items", [])]}

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
        data = await self.api.json(
            "POST",
            f"{CAL}/freeBusy",
            json={"timeMin": s.isoformat(), "timeMax": e.isoformat(), "items": [{"id": "primary"}]},
        )
        busy = [
            (parse_iso(b["start"], timezone), parse_iso(b["end"], timezone))
            for b in data.get("calendars", {}).get("primary", {}).get("busy", [])
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
        body = {
            "summary": title,
            "description": description or "",
            "location": location or "",
            "start": {"dateTime": local_wall(start_dt, zone), "timeZone": iana_tz(zone)},
            "end": {"dateTime": local_wall(end_dt, zone), "timeZone": iana_tz(zone)},
            "attendees": [{"email": a} for a in (attendees or []) if a],
        }
        e = await self.api.json(
            "POST",
            f"{CAL}/calendars/primary/events",
            params={"sendUpdates": "all" if send_invites else "none"},
            json=body,
        )
        return {**_event(e), "invites_sent": bool(send_invites and attendees), "timezone": tz_label(zone)}
