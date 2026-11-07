"""A short record of the tools a chat reply used, kept with the message so later turns know what already happened.

Chat history is rebuilt as text from the stored messages each turn, so before this a later turn saw only what the
assistant wrote ("Posted to #sales"), not which tools ran, with which ids, or whether an action was still waiting for
approval. The agent then re-ran lookups, lost ids it had found, and could not tell a posted message from a prepared
one. Each reply now stores one line per tool call: the tool, the identifying arguments, and the outcome. Recent
replies show it in full; older ones keep only the tool, its ids and the outcome.
"""

from __future__ import annotations

import json
import re
from typing import Any

MAX_CALLS = 12
SKIP_TOOLS = {"list_connectors", "search_tools", "get_tool_schema", "write_todos", "inspect_result", "task_catalog",
              "scratchpad_list", "list_my_skills", "skill_read", "skill_progress"}  # fmt: skip
_ID_KEY = re.compile(r"(^id$|_id$|^ids?$|url$|^number$|^slug$|^repo$|^branch$|^channel$|^to$|^thread|^message|^path$|^name$|^title$|^subject$|^email$)")
_STATUS_KEYS = ("status", "ok", "error", "posted", "merged", "sent", "count", "total", "done", "result")


def _short(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        value = " ".join(value.split())
        return value if len(value) <= limit else value[: limit - 1] + "…"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    text = json.dumps(value, default=str, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _compact_args(args: Any, brief: bool = False) -> dict:
    if not isinstance(args, dict):
        return {}
    out = {}
    for key, value in args.items():
        if brief and not _ID_KEY.search(str(key)):
            continue
        if isinstance(value, (dict, list)) and not _ID_KEY.search(str(key)):
            continue
        out[key] = _short(value, 60 if brief else 120)
        if len(out) >= (4 if brief else 8):
            break
    return out


def _outcome(content: Any) -> tuple[str, dict]:
    """("ran" | "error" | "waiting for the user's approval, not run", key facts) from a tool's result."""
    text = content if isinstance(content, str) else json.dumps(content, default=str)
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return "ran", {"result": _short(text, 160)}
    if isinstance(data, list):
        return "ran", {"items": len(data)}
    if not isinstance(data, dict):
        return "ran", {"result": _short(data, 160)}
    if data.get("status") in ("awaiting_user_confirmation", "awaiting_user") or data.get("needs_confirm"):
        return "waiting for the user's approval, not run", {}
    if data.get("approved_by_user") and isinstance(data.get("result"), str):
        status, facts = _outcome(data["result"])
        return ("ran after the user approved it" if status == "ran" else status), facts
    facts = {}
    for key, value in data.items():
        if key in _STATUS_KEYS or _ID_KEY.search(str(key)):
            if isinstance(value, (dict, list)):
                value = len(value) if isinstance(value, list) else _short(value, 80)
            facts[key] = _short(value, 80)
        if len(facts) >= 6:
            break
    return ("error" if data.get("error") else "ran"), facts


def build(messages: list) -> list[dict]:
    """One entry per tool call made after the user's latest message, oldest first."""
    start = 0
    for i in range(len(messages) - 1, -1, -1):
        if getattr(messages[i], "type", "") == "human":
            start = i + 1
            break
    results = {getattr(m, "tool_call_id", None): m for m in messages[start:] if getattr(m, "type", "") == "tool"}
    out: list[dict] = []
    for msg in messages[start:]:
        for call in getattr(msg, "tool_calls", None) or []:
            name, args = call.get("name") or "", call.get("args") or {}
            if name == "call_tool" and isinstance(args, dict):
                name = f"{args.get('connector_slug') or '?'}.{args.get('tool_name') or '?'}"
                args = args.get("args") or {}
            if name in SKIP_TOOLS:
                continue
            reply = results.get(call.get("id"))
            status, facts = _outcome(getattr(reply, "content", "")) if reply is not None else ("not finished", {})
            out.append({"tool": name, "args": _compact_args(args), "status": status, "facts": facts})
    return out[-MAX_CALLS:]


def render(digest: list[dict] | None, brief: bool = False) -> str:
    """Text for the history: full for recent replies, ids and outcomes only for older ones."""
    lines = []
    for item in digest or []:
        args = item.get("args") or {}
        if brief:
            args = {k: v for k, v in args.items() if _ID_KEY.search(str(k))}
        shown = ", ".join(f"{k}={v}" for k, v in args.items())
        facts = item.get("facts") or {}
        if brief:
            facts = {k: v for k, v in facts.items() if k in ("status", "ok", "error") or _ID_KEY.search(str(k))}
        tail = (": " + ", ".join(f"{k}={v}" for k, v in facts.items())) if facts else ""
        lines.append(f"- {item.get('tool')}({shown}) -> {item.get('status')}{tail}")
    if not lines:
        return ""
    return "[Tools this reply used]\n" + "\n".join(lines)
