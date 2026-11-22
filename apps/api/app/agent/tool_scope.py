"""Send the model only the tools a turn can use (docs/LLD.md "Lean context").

The agent has about a hundred built-in tools, and their schemas alone were ~16k tokens on every model call, whatever
the question. A sales question doesn't need the browser's fifteen tools, agent management or video generation. So a
chat turn starts with groups of specialised tools left out when nothing points at them, and:

- a group comes back when the message or the last replies point at it (a link, "fill the form", "make a deck"), when
  one of its tools was used earlier in the turn or in the last replies, or when a saved skill runs;
- the model can load a group itself with ``use_tools`` (the turn's context lists what is left out and why);
- a hidden tool that the model calls anyway still runs: only the schemas sent to the model change.

Background jobs, teammates and skill runs keep every tool. ``LEAN_CONTEXT=false`` turns it off.
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

# group -> (tool names, what it is for, words in a message that point at it). Generic signals only.
GROUPS: dict[str, tuple[re.Pattern, str, re.Pattern]] = {
    "browser": (
        re.compile(r"^(browser_|browse_)"),
        "operate websites in a real browser: open pages, click, fill forms, sign in, collect listings, download",
        re.compile(
            r"https?://|www\.|\b[\w-]+\.(?:com|in|org|net|io|co|ai|app)\b|\b(site|website|web ?page|page|browser|portal|"
            r"dashboard|log ?in|sign ?in|sign ?up|account|form|fill|apply|submit|click|tab|book|booking|flight|hotel|"
            r"ticket|cart|checkout|order|buy|purchase|price|compare|listing|scrape|crawl|download|screenshot|open)\b",
            re.I,
        ),
    ),
    "computer": (
        re.compile(r"^(computer_|launch_app$|clipboard_|open_path$|request_takeover$|save_upload_to_computer$)"),
        "use the computer itself: apps, windows, screen, clipboard, opening files",
        re.compile(r"\b(computer|desktop|mac|laptop|screen|window|app|application|clipboard|copy|paste|launch|open|"
                   r"finder|file|folder|take ?over)\b", re.I),  # fmt: skip
    ),
    "office": (
        re.compile(r"^(create_presentation|create_office_file|office_app)$"),
        "make or edit decks, documents and spreadsheets (pptx, docx, xlsx)",
        re.compile(r"\b(ppt|pptx|deck|slides?|presentation|docx?|word|excel|xlsx|sheet|spreadsheet|csv|pdf|report|"
                   r"document|export|file)\b", re.I),  # fmt: skip
    ),
    "media": (
        re.compile(r"^(generate_image|generate_video)$"),
        "generate images and videos",
        re.compile(r"\b(image|picture|photo|video|logo|draw|illustrat\w*|art|poster|thumbnail|banner|icon)\b", re.I),
    ),
    "research": (
        re.compile(r"^research_"),
        "long research across many sources: crawl, notes, saved summaries",
        re.compile(r"\b(research|crawl|sources|literature|in[- ]depth|thorough|comprehensive|study|whitepaper)\b", re.I),
    ),
    "agents": (
        re.compile(r"^(create_agent|update_agent|delete_agent|create_agent_group|delete_agent_group|add_group_members|"
                   r"remove_group_members|rename_agent_group|list_agents_and_groups)$"),  # fmt: skip
        "create, change or list workspace agents and groups",
        re.compile(r"\b(agents?|teammates?|groups?|team|staff|members?|hire|assistant named)\b", re.I),
    ),
    "skill_authoring": (
        re.compile(r"^(skill_create|skill_update|skill_delete|save_skill|start_teaching|stop_teaching)$"),
        "save, change or delete skills, and learn one by watching the user",
        re.compile(r"\b(skills?|workflow|routine|teach|learn|watch me|show you|remember how|automate|every time|"
                   r"save (?:this|that|it|these steps))\b", re.I),  # fmt: skip
    ),
    "mail_files": (
        re.compile(r"^save_mail_attachments$"),
        "save email attachments as files",
        re.compile(r"\b(attach\w*|e-?mails?|mail|inbox)\b", re.I),
    ),
}
LOAD_TOOL = "use_tools"


def group_of(name: str) -> str:
    for group, (names, _, _) in GROUPS.items():
        if names.match(name or ""):
            return group
    return ""


def _enabled() -> bool:
    from app.config import get_settings

    return bool(getattr(get_settings(), "lean_context", True))


def hidden_groups(query: str, history: list[dict] | None, plan_kind: str = "", full: bool = False) -> list[str]:
    """Groups this turn starts without. ``full`` (a job, a teammate, a saved skill) keeps everything."""
    if full or not _enabled():
        return []
    recent = [str(h.get("content") or "") for h in (history or [])[-4:]]
    text = "\n".join([query or "", *recent[-2:]])
    used = " ".join(recent)
    hidden = []
    for group, (names, _, words) in GROUPS.items():
        if words.search(text):
            continue
        if group in {"browser", "computer"} and plan_kind in {"computer"}:
            continue
        if group == "research" and plan_kind == "deep":
            continue
        if any(names.match(tok) for tok in re.findall(r"[a-z_]{4,}", used)):
            continue  # one of its tools ran in the last replies: a follow-up may need it again
        hidden.append(group)
    return hidden


def context_note(hidden: list[str]) -> str:
    if not hidden:
        return ""
    lines = "\n".join(f"- {g}: {GROUPS[g][1]}" for g in hidden)
    return (
        f"Tools left out of this turn to keep it fast (call {LOAD_TOOL} with the group name if the task needs one; "
        f"never tell the user you can't do something because of this):\n{lines}"
    )


def load_tool(hidden: list[str]):
    """The ``use_tools`` tool. Its call is what makes the middleware send that group from the next model call on."""
    from langchain_core.tools import StructuredTool

    async def use_tools(group: str) -> dict:
        group = (group or "").strip().lower()
        if group not in GROUPS:
            return {"error": f"Unknown group. Groups: {', '.join(hidden or GROUPS)}"}
        out = {"loaded": group, "note": "Those tools are available from your next step."}
        from app.agent import prompt_rules

        rules = prompt_rules.for_group(group)  # left out of the system prompt with the tools (app/agent/prompt_rules.py)
        if rules:
            out["rules"] = rules
        return out

    return StructuredTool.from_function(
        coroutine=use_tools,
        name=LOAD_TOOL,
        description="Load a group of tools left out of this turn: " + ", ".join(hidden or GROUPS) + ".",
    )


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        return str(tool.get("name") or (tool.get("function") or {}).get("name") or "")
    return str(getattr(tool, "name", "") or "")


def visible_hidden(hidden: list[str], messages: list) -> set[str]:
    """Groups still hidden for this model call: minus any loaded with use_tools or used earlier in the turn."""
    left = set(hidden)
    for m in messages:
        for call in getattr(m, "tool_calls", None) or []:
            name = str(call.get("name") or "")
            if name == LOAD_TOOL:
                left.discard(str((call.get("args") or {}).get("group") or "").strip().lower())
            else:
                left.discard(group_of(name))
    return left


def scope_tools(tools: list, hidden: set[str]) -> list:
    if not hidden:
        return list(tools)
    return [t for t in tools if group_of(_tool_name(t)) not in hidden]


def middleware(hidden: list[str]):
    from langchain.agents.middleware import AgentMiddleware

    class ToolScope(AgentMiddleware):
        def _request(self, request):  # noqa: ANN001
            left = visible_hidden(hidden, list(request.messages))
            if not left:
                return request
            tools = scope_tools(list(request.tools or []), left)
            return request.override(tools=tools) if len(tools) != len(request.tools or []) else request

        def wrap_model_call(self, request, handler):  # noqa: ANN001
            return handler(self._request(request))

        async def awrap_model_call(self, request, handler):  # noqa: ANN001
            return await handler(self._request(request))

    return ToolScope()


STICKY_TTL_S = 6 * 3600


async def sticky(session_id, hidden: list[str], skip: bool = False) -> list[str]:
    """Keep a conversation's tool list stable: a group shown once in this chat stays shown for its later turns.

    The provider caches a prompt only up to the first difference, and the tool schemas come before everything else, so
    a tool list that changed every turn re-billed the whole fixed prompt. Growing it only once per group keeps the cache
    hit on the turns after. Never raises: without Redis each turn scopes on its own, as before.
    """
    if skip or session_id is None:
        return hidden
    try:
        from app.cache import get_redis

        r = get_redis()
        key = f"tools-shown:{session_id}"
        shown = {x.decode() if isinstance(x, bytes) else str(x) for x in (await r.smembers(key) or set())}
        kept = [g for g in hidden if g not in shown]
        visible = [g for g in GROUPS if g not in kept]
        if visible:
            await r.sadd(key, *visible)
            await r.expire(key, STICKY_TTL_S)
        return kept
    except Exception:  # noqa: BLE001
        return hidden
