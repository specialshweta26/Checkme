"""Tools implemented by the API on top of a mail connector (they need the LLM or object storage)."""

from __future__ import annotations

from connector_core import ToolSpec

SUMMARIZE_THREAD = ToolSpec(
    "summarize_thread",
    "Summarize a whole email conversation: decisions, open questions, action items with owners, and who said what. "
    "Works for very long threads. Cached until a new reply arrives.",
    {
        "type": "object",
        "properties": {"thread_id": {"type": "string"}, "focus": {"type": "string", "description": "Optional focus, e.g. 'pricing'"}},
        "required": ["thread_id"],
        "additionalProperties": False,
    },
    group="threads",
    requires=("thread_id",),
    tags=("mail",),
)

READ_ATTACHMENT_TEXT = ToolSpec(
    "read_attachment_text",
    "Extract and read the text of an attachment (pdf, docx, txt, csv, html). Text is clipped.",
    {
        "type": "object",
        "properties": {"message_id": {"type": "string"}, "attachment_id": {"type": "string"}},
        "required": ["message_id", "attachment_id"],
        "additionalProperties": False,
    },
    group="attachments",
    requires=("message_id", "attachment_id"),
    tags=("mail", "files"),
)

SUMMARIZE_ATTACHMENTS = ToolSpec(
    "summarize_attachments",
    "Download every attachment on a message, extract text, and summarize. Also returns preview and download links.",
    {
        "type": "object",
        "properties": {
            "message_id": {"type": "string"},
            "focus": {"type": "string", "description": "Optional focus, e.g. 'balances'"},
        },
        "required": ["message_id"],
        "additionalProperties": False,
    },
    group="attachments",
    requires=("message_id",),
    tags=("mail", "files"),
)

TODAY_BRIEFING = ToolSpec(
    "today_briefing",
    "Chief of staff briefing: what matters today, calendar risk, people waiting on a reply, open commitments, bills, and next actions.",
    {"type": "object", "properties": {}, "additionalProperties": False},
    group="staff",
    tags=("mail", "calendar"),
)

ASK_INBOX = ToolSpec(
    "ask_inbox",
    "Answer a natural-language question about the user's email (waiting on, promises, pending decisions) and cite source message ids.",
    {
        "type": "object",
        "properties": {"question": {"type": "string"}},
        "required": ["question"],
        "additionalProperties": False,
    },
    group="staff",
    tags=("mail",),
)

SCAN_COMMITMENTS = ToolSpec(
    "scan_commitments",
    "Scan recent mail for promises and deadlines and save them to the commitment tracker.",
    {"type": "object", "properties": {}, "additionalProperties": False},
    group="staff",
    tags=("mail",),
)

LIST_COMMITMENTS = ToolSpec(
    "list_commitments",
    "List open commitments the assistant is tracking, with due dates and source emails.",
    {"type": "object", "properties": {}, "additionalProperties": False},
    group="staff",
    tags=("mail",),
)

SCAN_ADMIN = ToolSpec(
    "scan_admin",
    "Find receipts, invoices, bills, and subscription renewals in mail and save them for reminders.",
    {"type": "object", "properties": {}, "additionalProperties": False},
    group="staff",
    tags=("mail",),
)

SCHEDULE_TASK = ToolSpec(
    "schedule_task",
    "Create a timed task from the user's natural-language request. Use when they want work done later or on a repeating clock (in N minutes, tonight, every day, every week). The assistant runs the instruction at the scheduled time.",
    {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "The user's full request, including what to do and when."},
        },
        "required": ["prompt"],
        "additionalProperties": False,
    },
    group="staff",
    read_only=False,
    tags=("mail",),
)

LIST_SCHEDULED_TASKS = ToolSpec(
    "list_scheduled_tasks",
    "List this user's scheduled tasks with next run time, status, and last result.",
    {"type": "object", "properties": {}, "additionalProperties": False},
    group="staff",
    tags=("mail",),
)

UPDATE_SCHEDULED_TASK = ToolSpec(
    "update_scheduled_task",
    "Pause or resume a scheduled task.",
    {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "status": {"type": "string", "enum": ["active", "paused"]},
        },
        "required": ["task_id", "status"],
        "additionalProperties": False,
    },
    group="staff",
    read_only=False,
    tags=("mail",),
)

CREATE_AUTOMATION = ToolSpec(
    "create_automation",
    "Turn a plain-English workflow into a stored automation with trigger, condition, action, and approval requirement.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "trigger": {"type": "string"},
            "condition": {"type": "string"},
            "action": {"type": "string"},
            "requires_approval": {"type": "boolean", "default": True},
        },
        "required": ["name", "trigger", "action"],
        "additionalProperties": False,
    },
    group="staff",
    read_only=False,
    tags=("mail",),
)

LIST_AUTOMATIONS = ToolSpec(
    "list_automations",
    "List this user's saved natural-language automations.",
    {"type": "object", "properties": {}, "additionalProperties": False},
    group="staff",
    tags=("mail",),
)

LEARN_VOICE = ToolSpec(
    "learn_voice",
    "Learn how the user writes from sent mail and save a communication twin used for future drafts.",
    {"type": "object", "properties": {}, "additionalProperties": False},
    group="staff",
    tags=("mail",),
)

VIRTUAL_MAIL_TOOLS = [
    SUMMARIZE_THREAD,
    READ_ATTACHMENT_TEXT,
    SUMMARIZE_ATTACHMENTS,
    TODAY_BRIEFING,
    ASK_INBOX,
    SCAN_COMMITMENTS,
    LIST_COMMITMENTS,
    SCAN_ADMIN,
    SCHEDULE_TASK,
    LIST_SCHEDULED_TASKS,
    UPDATE_SCHEDULED_TASK,
    CREATE_AUTOMATION,
    LIST_AUTOMATIONS,
    LEARN_VOICE,
]
VIRTUAL_NAMES = {t.name for t in VIRTUAL_MAIL_TOOLS}
