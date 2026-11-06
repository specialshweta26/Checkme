from __future__ import annotations

import re

_SPLIT = re.compile(
    r"\s+(?:and then|then|and also|also|and about|as well as)\s+|,\s+(?=[a-z])|\s+and\s+(?=about\b|the next\b|next\b)",
    re.I,
)
_FOLLOWUP = re.compile(
    r"\b(those|them|that|it|the (first|second|third|last) one|same|again|reply|delete those|archive those|confirmed|confirm|continue|month on month|mom|by month|monthly|week on week|breakdown|split by)\b",
    re.I,
)
_EXPLICIT = re.compile(r"@([a-z0-9][a-z0-9_-]{1,31})\b", re.I)
_FIRST_PARTY = [
    (re.compile(r"\b(summarize|summary of).*(thread|conversation|email)\b", re.I), "thread_summary", "mail"),
    (re.compile(r"\b(delete|trash|purge).*(mail|email|message|inbox|promo)\b", re.I), "bulk_delete", "mail"),
    (re.compile(r"\b(attachment|pdf|download file|what does .{0,40} say)\b", re.I), "attachment", "files"),
    (re.compile(r"\b(invite|meeting invite|schedule|book a|create (an )?event)\b", re.I), "calendar_write", "calendar"),
    (re.compile(r"\b(calendar|meeting|agenda|free slot|available|event|am i free|free)\b", re.I), "calendar", "calendar"),
    (re.compile(r"\b(briefing|what matters|waiting for|promised|commitment|chief of staff)\b", re.I), "staff", "mail"),
    (re.compile(r"\b(invoice|receipt|subscription|renewal|bill)\b", re.I), "admin", "mail"),
    (re.compile(r"\b(whenever|every time|when I (get|receive)|automat)\b", re.I), "automation", "mail"),
    (re.compile(r"\b(sound like me|writing style|my voice|draft like me)\b", re.I), "voice", "mail"),
    (re.compile(r"\b(send|reply|forward|draft|compose|email .+)\b", re.I), "mail_write", "mail"),
    (re.compile(r"\b(inbox|unread|mail|email|thread|from:|subject:)\b", re.I), "mail_read", "mail"),
]


def explicit_slugs(text: str) -> list[str]:
    return [m.group(1).lower() for m in _EXPLICIT.finditer(text)]


def is_followup(text: str) -> bool:
    return bool(_FOLLOWUP.search(text)) and len(text.split()) < 14


def first_party_tag(text: str) -> tuple[str, str] | None:
    for pattern, intent, tag in _FIRST_PARTY:
        if pattern.search(text):
            return intent, tag
    return None


def split_subtasks(text: str) -> list[str]:
    parts = [p.strip() for p in _SPLIT.split(text) if p.strip()]
    return parts if len(parts) > 1 else [text.strip()]
