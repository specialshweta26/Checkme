"""Tool specs shared by every mail + calendar connector, so Gmail and Outlook expose identical names."""

from __future__ import annotations

from connector_core.spec import ToolSpec


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


_STR = {"type": "string"}
_IDS = {"type": "array", "items": {"type": "string"}}
_EMAILS = {"type": "array", "items": {"type": "string"}, "description": "Email addresses"}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": 50, "default": 15}
_ISO = {"type": "string", "description": "ISO 8601 datetime, e.g. 2026-10-19T09:00:00+05:30"}

MAIL_CALENDAR_TOOLS: list[ToolSpec] = [
    ToolSpec(
        "list_messages",
        "List recent messages in a folder/label (default inbox), newest first. Returns ids, subject, sender, date, snippet.",
        _obj(
            {
                "folder": {**_STR, "description": "Folder or label id; default inbox"},
                "unread_only": {"type": "boolean", "default": False},
                "limit": _LIMIT,
                "page_token": _STR,
            }
        ),
        group="messages.read",
        produces=("message_id", "thread_id"),
        tags=("mail",),
    ),
    ToolSpec(
        "search_messages",
        "Search mail by keywords, sender, subject, or date. Use provider-neutral words; e.g. 'from:acme invoice after:2026-07-01'.",
        _obj({"query": _STR, "limit": _LIMIT, "page_token": _STR}, ["query"]),
        group="messages.read",
        produces=("message_id", "thread_id"),
        tags=("mail",),
    ),
    ToolSpec(
        "get_message",
        "Read one message: headers, cleaned body (clipped), attachment list.",
        _obj({"message_id": _STR}, ["message_id"]),
        group="messages.read",
        requires=("message_id",),
        produces=("thread_id", "attachment_id"),
        tags=("mail",),
    ),
    ToolSpec(
        "get_thread",
        "Read a whole conversation oldest-first with quoted replies and signatures removed.",
        _obj({"thread_id": _STR, "max_messages": {"type": "integer", "default": 50, "maximum": 200}}, ["thread_id"]),
        group="threads",
        requires=("thread_id",),
        produces=("message_id",),
        tags=("mail",),
    ),
    ToolSpec(
        "list_folders",
        "List mail folders (Outlook) or labels (Gmail) with ids.",
        _obj({}),
        group="folders",
        tags=("mail",),
    ),
    ToolSpec(
        "mark_read",
        "Mark messages read or unread.",
        _obj({"message_ids": _IDS, "read": {"type": "boolean", "default": True}}, ["message_ids"]),
        group="messages.manage",
        read_only=False,
        requires=("message_id",),
        tags=("mail",),
    ),
    ToolSpec(
        "archive_messages",
        "Archive messages (remove from inbox without deleting).",
        _obj({"message_ids": _IDS}, ["message_ids"]),
        group="messages.manage",
        read_only=False,
        requires=("message_id",),
        tags=("mail",),
    ),
    ToolSpec(
        "create_draft",
        "Create a draft email (not sent).",
        _obj({"to": _EMAILS, "cc": _EMAILS, "subject": _STR, "body": _STR}, ["to", "subject", "body"]),
        group="messages.write",
        read_only=False,
        tags=("mail",),
    ),
    ToolSpec(
        "send_message",
        "Send a new email. Requires user confirmation in the app.",
        _obj({"to": _EMAILS, "cc": _EMAILS, "bcc": _EMAILS, "subject": _STR, "body": _STR}, ["to", "subject", "body"]),
        group="messages.write",
        read_only=False,
        destructive=True,
        tags=("mail",),
    ),
    ToolSpec(
        "reply",
        "Reply to a message (optionally reply-all). Requires user confirmation in the app.",
        _obj({"message_id": _STR, "body": _STR, "reply_all": {"type": "boolean", "default": False}}, ["message_id", "body"]),
        group="messages.write",
        read_only=False,
        destructive=True,
        requires=("message_id",),
        tags=("mail",),
    ),
    ToolSpec(
        "forward",
        "Forward a message with an optional comment. Requires user confirmation in the app.",
        _obj({"message_id": _STR, "to": _EMAILS, "comment": _STR}, ["message_id", "to"]),
        group="messages.write",
        read_only=False,
        destructive=True,
        requires=("message_id",),
        tags=("mail",),
    ),
    ToolSpec(
        "bulk_delete",
        "Delete many messages by ids or by a search query (moves to trash; permanent=true purges). "
        "Always previews first and requires user confirmation in the app. Max 1000 per run.",
        _obj(
            {
                "message_ids": _IDS,
                "query": {**_STR, "description": "Search query selecting the messages to delete"},
                "permanent": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 500},
            }
        ),
        group="messages.manage",
        read_only=False,
        destructive=True,
        tags=("mail",),
    ),
    ToolSpec(
        "list_attachments",
        "List attachments of a message: id, filename, mime type, size.",
        _obj({"message_id": _STR}, ["message_id"]),
        group="attachments",
        requires=("message_id",),
        produces=("attachment_id",),
        tags=("mail", "files"),
    ),
    ToolSpec(
        "download_attachment",
        "Download an attachment. The user receives a secure download link; file bytes never go to the model.",
        _obj({"message_id": _STR, "attachment_id": _STR}, ["message_id", "attachment_id"]),
        group="attachments",
        requires=("message_id", "attachment_id"),
        tags=("mail", "files"),
    ),
    ToolSpec(
        "list_events",
        "List calendar events between start and end (recurring events expanded).",
        _obj({"start": _ISO, "end": _ISO, "limit": {"type": "integer", "default": 25, "maximum": 100}}, ["start", "end"]),
        group="calendar",
        produces=("event_id",),
        tags=("calendar",),
    ),
    ToolSpec(
        "get_event",
        "Get details of one calendar event: attendees, location, meeting link, description.",
        _obj({"event_id": _STR}, ["event_id"]),
        group="calendar",
        requires=("event_id",),
        tags=("calendar",),
    ),
    ToolSpec(
        "search_events",
        "Search calendar events by text within an optional date range (default: next 30 days).",
        _obj({"query": _STR, "start": _ISO, "end": _ISO}, ["query"]),
        group="calendar",
        produces=("event_id",),
        tags=("calendar",),
    ),
    ToolSpec(
        "create_event",
        "Create a calendar event and send meeting invites to attendees. Requires user confirmation. "
        "Interpret local times as IST (Asia/Kolkata) unless another timezone is given. Always include start, end, title, and attendees.",
        _obj(
            {
                "title": _STR,
                "start": _ISO,
                "end": _ISO,
                "duration_minutes": {"type": "integer", "default": 30, "minimum": 15, "maximum": 480},
                "attendees": _EMAILS,
                "description": _STR,
                "location": _STR,
                "timezone": {**_STR, "description": "IANA zone; default Asia/Kolkata (IST)", "default": "Asia/Kolkata"},
                "send_invites": {"type": "boolean", "default": True},
            },
            ["title", "start"],
        ),
        group="calendar",
        read_only=False,
        destructive=True,
        produces=("event_id",),
        tags=("calendar",),
    ),
    ToolSpec(
        "find_free_slots",
        "Find free time slots on weekdays within working hours.",
        _obj(
            {
                "start": _ISO,
                "end": _ISO,
                "duration_minutes": {"type": "integer", "default": 30},
                "work_hours_start": {**_STR, "default": "09:00"},
                "work_hours_end": {**_STR, "default": "18:00"},
                "timezone": {**_STR, "description": "IANA zone; default Asia/Kolkata (IST)", "default": "Asia/Kolkata"},
            },
            ["start", "end"],
        ),
        group="calendar",
        tags=("calendar",),
    ),
]
