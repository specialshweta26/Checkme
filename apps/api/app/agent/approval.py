"""Approval rules for actions the agent takes on the user's behalf.

1. Tools nobody vouched for need approval. A user-added MCP tool is read-only only when its server says so
   (``readOnlyHint: true``), and a marketplace (gateway) action runs without a card only when its name is plainly a
   read (``GMAIL_FETCH_EMAILS``), never when it writes (``SLACK_SEND_MESSAGE``) or is unclear.
2. An approval covers one exact action: the same connector, tool and arguments, once.
3. Runs nobody is watching (schedules, events) approve their own destructive actions only before any third-party
   content (mail, web pages, MCP results, webhook payloads) is in the turn, or for mail addressed only to the user.
4. In a job's chat only a plain yes approves; anything conditional or questioning is asked again.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.routing.retrieve import is_catalog_tool, is_execute_tool

# --- 1. what needs a card -----------------------------------------------------------------------------------------

_WORDS = re.compile(r"[a-z0-9]+")
READ_WORDS = frozenset(
    {
        "get", "list", "search", "fetch", "read", "find", "retrieve", "query", "describe", "count", "check",
        "view", "show", "lookup", "browse", "preview", "inspect", "stat", "stats", "summary", "summarize",
    }
)
WRITE_WORDS = frozenset(
    {
        "create", "send", "post", "update", "delete", "remove", "add", "set", "put", "patch", "write", "upload",
        "invite", "share", "publish", "merge", "move", "archive", "approve", "cancel", "pay", "charge", "transfer",
        "assign", "close", "reopen", "star", "unstar", "follow", "unfollow", "reply", "forward", "submit", "edit",
        "rename", "replace", "insert", "import", "trigger", "run", "execute", "start", "stop", "restart", "deploy",
        "book", "order", "purchase", "refund", "accept", "decline", "reject", "mark", "label", "tag", "enable",
        "disable", "grant", "revoke", "kick", "ban", "mute", "pin", "unpin", "schedule", "draft", "comment",
        "react", "vote", "subscribe", "unsubscribe", "empty", "purge", "destroy", "drop", "reset", "clear", "push",
    }
)


def action_words(name: str) -> set[str]:
    return set(_WORDS.findall((name or "").lower()))


def is_read_action(name: str) -> bool:
    """True only for a name that plainly reads: a read verb and no write verb (``GITHUB_GET_A_REPOSITORY``)."""
    words = action_words(name)
    return bool(words & READ_WORDS) and not (words & WRITE_WORDS)


def nested_actions(tool_name: str, args: dict | None) -> list[str]:
    """The real action(s) behind a gateway execute call; otherwise the tool itself."""
    args = args if isinstance(args, dict) else {}
    if not is_execute_tool(tool_name):
        return [tool_name]
    names: list[str] = []
    for key in ("tool_name", "tool_slug", "name", "action"):
        if isinstance(args.get(key), str) and args[key].strip():
            names.append(args[key].strip())
    for item in args.get("tools") or []:
        if isinstance(item, dict):
            for key in ("tool_slug", "tool_name", "name", "action"):
                if isinstance(item.get(key), str) and item[key].strip():
                    names.append(item[key].strip())
                    break
    return names or [tool_name]


def gateway_needs_approval(tool_name: str, args: dict | None) -> bool:
    """A marketplace/gateway action (no stored flags, or a gateway's execute tool): a card unless every action is
    plainly a read. Discovery tools (search, resolve, list) never need one. Execute is checked first: Composio's
    execute tools are also on the catalog list."""
    if not is_execute_tool(tool_name) and is_catalog_tool(tool_name):
        return False
    actions = nested_actions(tool_name, args)
    if any(is_execute_tool(name) for name in actions):
        return True  # an execute call whose action we can't see
    return not all(is_read_action(name) for name in actions)


def mcp_needs_approval(read_only: bool, destructive: bool) -> bool:
    """Stored flags for a user-added MCP tool: anything not explicitly read-only needs a card."""
    return destructive or not read_only


# --- 2. one approval, one exact action ----------------------------------------------------------------------------


def canonical_args(args: Any) -> str:
    return json.dumps(args if args is not None else {}, sort_keys=True, default=str, separators=(",", ":"))


def matches(confirmation: dict, slug: str, tool: str, args: Any) -> bool:
    return (
        confirmation.get("slug") == slug
        and confirmation.get("tool") == tool
        and canonical_args(confirmation.get("args") or {}) == canonical_args(args or {})
    )


def find_approval(confirmations: list[dict], slug: str, tool: str, args: Any) -> dict | None:
    return next((c for c in confirmations or [] if matches(c, slug, tool, args)), None)


async def claim_token(token: str, exp: int | float | None, redis: Any = None) -> bool:
    """First use of a signed approval token wins; a second request with the same token (a double click, a retried
    request) does not run the action again. Without Redis, the token is accepted as before."""
    import hashlib
    import time

    key = "approval_used:" + hashlib.sha256((token or "").encode()).hexdigest()[:32]
    ttl = max(60, int((exp or time.time() + 600) - time.time()) + 60)
    try:
        if redis is None:
            from app.cache import get_redis

            redis = get_redis()
        return bool(await redis.set(key, b"1", nx=True, ex=ttl))
    except Exception:  # noqa: BLE001 - a Redis outage must not block approved actions
        return True


def consume(confirmations: list[dict], approval: dict | None) -> None:
    """Use an approval up: the same action needs a new one."""
    if approval is None:
        return
    for i, item in enumerate(confirmations):
        if item is approval:
            del confirmations[i]
            return


# --- 3. runs nobody is watching -----------------------------------------------------------------------------------

SEND_TOOLS = frozenset({"send_message", "send_email", "send_mail"})
_ADDRESS = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def recipients(args: dict | None) -> list[str]:
    args = args if isinstance(args, dict) else {}
    found: list[str] = []
    for key in ("to", "cc", "bcc", "recipients", "recipient", "to_address", "email"):
        value = args.get(key)
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict):
                item = item.get("email") or item.get("address") or ""
            found.extend(a.lower() for a in _ADDRESS.findall(str(item or "")))
    return found


def addressed_only_to(tool_name: str, args: dict | None, own: set[str]) -> bool:
    """A new message whose every recipient is one of the user's own addresses (a report the user scheduled)."""
    if tool_name not in SEND_TOOLS:
        return False
    people = recipients(args)
    return bool(people) and all(p in own for p in people)


# --- 4. a plain yes in a job's chat --------------------------------------------------------------------------------

APPROVE_REPLIES = frozenset(
    {
        "yes", "y", "yeah", "yep", "yup", "sure", "ok", "okay", "approve", "approved", "i approve", "confirm",
        "confirmed", "go ahead", "go for it", "do it", "proceed", "please proceed", "please go ahead", "please do",
        "please do it", "yes please", "yes, please", "yes go ahead", "yes, go ahead", "yes do it", "yes, do it",
        "sounds good", "that's fine", "that is fine", "send it", "ship it", "merge it", "deploy it", "lgtm",
        "absolutely", "of course", "continue", "yes, proceed", "yes proceed", "ok go ahead", "ok, go ahead",
        "okay go ahead", "okay, go ahead",
    }
)
DECLINE_PREFIXES = (
    "no", "nope", "don't", "do not", "i don't", "i do not", "please don't", "please do not", "leave it", "leave that",
    "cancel", "stop", "skip", "not now", "i decline", "decline", "reject", "hold off", "wait",
)


def normalize_reply(text: str) -> str:
    out = " ".join((text or "").lower().strip().replace("’", "'").split())
    return re.sub(r"[.!]+$", "", out).strip()


def reply_intent(text: str) -> str:
    """"approve" only for a plain yes on its own; "decline" for a no; otherwise "unclear" (ask again)."""
    said = normalize_reply(text)
    if not said:
        return "unclear"
    if any(said == p or said.startswith(p + ",") or said.startswith(p + " ") for p in DECLINE_PREFIXES):
        return "decline"
    if said in APPROVE_REPLIES:
        return "approve"
    return "unclear"
