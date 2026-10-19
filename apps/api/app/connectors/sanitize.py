from __future__ import annotations

import re

_INJECT = re.compile(
    r"(?is)(ignore (all|previous) instructions|you are now|system prompt|do not follow|jailbreak|"
    r"<script|javascript:)"
)
_SPACES = re.compile(r"\s+")


def clean_description(text: str, limit: int = 300) -> str:
    text = _SPACES.sub(" ", (text or "").strip())
    text = _INJECT.sub("[removed]", text)
    return text[:limit]


def infer_group(name: str, tags: list[str] | None = None) -> str:
    n = name.lower()
    if any(k in n for k in ("delete", "remove", "purge", "trash")):
        return "destructive"
    if any(k in n for k in ("send", "reply", "forward", "create", "update", "write", "post")):
        return "write"
    if any(k in n for k in ("attach", "file", "download", "upload")):
        return "attachments"
    if any(k in n for k in ("event", "calendar", "meeting", "slot")):
        return "calendar"
    if any(k in n for k in ("search", "list", "get", "read", "find")):
        return "read"
    if tags:
        return tags[0]
    prefix = name.split("_", 1)[0]
    return prefix or "other"


def infer_ids(name: str, schema: dict) -> tuple[list[str], list[str]]:
    props = (schema or {}).get("properties") or {}
    names = set(props) | {name.lower()}
    requires, produces = [], []
    for kind in ("message_id", "thread_id", "attachment_id", "event_id"):
        short = kind.replace("_id", "")
        if kind in names or f"{short}_id" in names or short in name.lower():
            if kind in props or f"{short}s" in props:
                requires.append(kind)
            if any(k in name.lower() for k in ("list", "search", "get", "create")):
                produces.append(kind)
    return requires, produces


def destructive_name(name: str) -> bool:
    n = name.lower()
    return any(k in n for k in ("delete", "remove", "purge", "send", "transfer", "destroy"))
