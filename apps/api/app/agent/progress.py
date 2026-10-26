"""Live, task-grounded progress narration for chat turns.

The chat UI used to cycle canned phrases ("Thinking", "Checking connected sources",
"Writing a reply") on a timer. Instead, the agent loop now reports what it is really
doing: each tool call, connector, search query, file, teammate hand-off and model step
becomes one short human line. Lines are derived from real LangChain callback events
(no extra LLM calls) and buffered in Redis so the web app can poll them while the
POST /chat request is still running.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any
from urllib.parse import urlparse

from langchain_core.callbacks import AsyncCallbackHandler

log = logging.getLogger(__name__)

PROGRESS_TTL_S = 900
MAX_STEPS = 60
PROGRESS_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

# Words that add nothing to a humanized tool name.
_NOISE = {"tool", "tools", "action", "v1", "v2", "api", "get", "list", "fetch"}
_KEY_ARGS = (
    "query",
    "q",
    "search",
    "search_query",
    "question",
    "url",
    "subject",
    "title",
    "name",
    "repo",
    "path",
    "file_path",
    "to",
    "prompt",
    "text",
)
_SKIP_TOOLS = {"inspect_result_internal"}


def valid_progress_id(value: str | None) -> bool:
    return bool(value) and bool(PROGRESS_ID_RE.match(str(value)))


def _key(user_id: Any, progress_id: str) -> str:
    return f"chatprog:{user_id}:{progress_id}"


def _clip(value: Any, n: int = 60) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _quote(value: Any, n: int = 50) -> str:
    text = _clip(value, n)
    return f"“{text}”" if text else ""


def humanize_tool(name: str, connector: str = "") -> str:
    """GMAIL_SEARCH_MESSAGES -> 'search messages'; list_events -> 'list events'."""
    raw = str(name or "").strip()
    if "__" in raw:
        raw = raw.split("__")[-1]
    words = [w for w in re.split(r"[_\-\s.]+|(?<=[a-z])(?=[A-Z])", raw) if w]
    words = [w.lower() for w in words]
    prefix = re.split(r"[_\-\s]+", (connector or "").lower())
    while words and prefix and words[0] in prefix:
        words.pop(0)
    kept = [w for w in words if w not in _NOISE] or words
    return " ".join(kept[:5]) or "run a tool"


def _key_arg(args: dict | None) -> tuple[str, Any]:
    if not isinstance(args, dict):
        return "", None
    for k in _KEY_ARGS:
        v = args.get(k)
        if isinstance(v, (str, int, float)) and str(v).strip():
            return k, v
    # One level of nesting (e.g. gateway__execute {"arguments": {...}}).
    for v in args.values():
        if isinstance(v, dict):
            k2, v2 = _key_arg(v)
            if v2 is not None:
                return k2, v2
    return "", None


def _host(url: Any) -> str:
    try:
        host = urlparse(str(url)).netloc or str(url)
    except Exception:  # noqa: BLE001
        host = str(url)
    return host.removeprefix("www.")


def describe_connector_call(connector: str, tool: str, args: dict | None) -> str:
    """One line for call_tool(connector_slug, tool_name, args)."""
    conn = connector or "a connected app"
    t = (tool or "").lower()
    args = args if isinstance(args, dict) else {}
    if t.endswith("tool_search") or t in {"find_tools", "list_apps", "resolve"} or t.endswith("__resolve"):
        _, q = _key_arg(args)
        return f"Finding the right {conn} tool" + (f" for {_quote(q)}" if q else "")
    if "navigate" in t or t in {"open_url", "web_fetch", "fetch_url"}:
        url = args.get("url") or _key_arg(args)[1]
        return f"Opening {_host(url)}" + (" in the browser" if "browser" in t else "") if url else f"Opening a page with {conn}"
    if "screenshot" in t:
        return "Taking a screenshot of the screen"
    if "click" in t:
        return "Clicking on the page"
    if t.startswith("browser_type") or "type_text" in t or t.endswith("_type"):
        return "Typing into the page"
    if "send" in t and ("mail" in t or "message" in t or args.get("to")):
        to = args.get("to")
        to = ", ".join(map(str, to)) if isinstance(to, list) else to
        return f"Preparing a message on {conn}" + (f" to {_clip(to, 40)}" if to else "")
    action = humanize_tool(tool, connector)
    k, v = _key_arg(args)
    detail = ""
    if v is not None:
        detail = f" {_host(v)}" if k == "url" else (f" for {_quote(v)}" if k in {"query", "q", "search", "search_query", "question"} else f": {_clip(v, 48)}")
    return f"{conn}: {action}{detail}"


def describe_tool(name: str, args: dict | None, connector_names: dict[str, str] | None = None) -> str | None:
    """Turn one real tool call into a short present-tense status line."""
    names = connector_names or {}
    a = args if isinstance(args, dict) else {}
    n = (name or "").strip()
    if not n or n in _SKIP_TOOLS:
        return None

    def conn_name(slug: Any) -> str:
        s = str(slug or "")
        return names.get(s) or s.replace("-", " ").replace("_", " ").title() or "a connected app"

    if n == "list_connectors":
        return "Checking which apps you have connected"
    if n == "search_tools":
        q = a.get("query")
        where = f" in {conn_name(a['connector_slug'])}" if a.get("connector_slug") else ""
        return f"Looking for a tool{where} that can {_clip(q, 50)}" if q else f"Looking through your tools{where}"
    if n == "get_tool_schema":
        return f"Reading how to use {conn_name(a.get('connector_slug'))} · {humanize_tool(a.get('tool_name') or '', a.get('connector_slug') or '')}"
    if n == "call_tool":
        return describe_connector_call(conn_name(a.get("connector_slug")), a.get("tool_name") or "", a.get("args"))
    if n == "inspect_result":
        what = a.get("search") or a.get("path")
        return f"Reading the results for {_quote(what)}" if what else "Reading through the results"
    if n == "schedule_task":
        return f"Scheduling: {_clip(a.get('prompt'), 60)}"
    if n == "run_staff_tool":
        staff = str(a.get("name") or "")
        return {
            "today_briefing": "Putting together today's briefing",
            "ask_inbox": "Searching your inbox",
            "scan_commitments": "Scanning mail for commitments",
            "list_commitments": "Listing your commitments",
            "scan_admin": "Scanning for admin to-dos",
            "list_scheduled_tasks": "Checking your routines",
            "update_scheduled_task": "Updating a routine",
            "create_automation": "Setting up an automation",
            "list_automations": "Checking your automations",
            "learn_voice": "Learning your writing voice",
        }.get(staff, f"Running {humanize_tool(staff)}")
    if n == "ask_teammate":
        return f"Handing work to a teammate: {_clip(a.get('prompt'), 60)}"
    if n == "list_agents_and_groups":
        return "Looking up your agents and groups"
    if n == "create_agent":
        return f"Creating the agent {_quote(a.get('name'))}".rstrip()
    if n == "update_agent":
        return f"Updating the agent {_quote(a.get('agent'))}".rstrip()
    if n == "create_agent_group":
        who = "all your agents" if a.get("all_agents") else ", ".join(map(str, a.get("members") or []))[:60]
        return f"Creating the group {_quote(a.get('name'))}" + (f" with {who}" if who else "")
    if n == "add_group_members":
        return f"Adding {', '.join(map(str, a.get('members') or []))[:60]} to {_quote(a.get('group'))}"
    if n == "remove_group_members":
        return f"Removing {', '.join(map(str, a.get('members') or []))[:60]} from {_quote(a.get('group'))}"
    if n == "rename_agent_group":
        return f"Renaming {_quote(a.get('group'))} to {_quote(a.get('name'))}"
    if n in {"delete_agent", "delete_agent_group"}:
        return f"Preparing a delete confirmation for {_quote(a.get('target'))}"
    if n == "set_my_focus":
        from app.agents.focus import FOCUSES

        f = str(a.get("focus") or "")
        return f"Setting my focus to {FOCUSES[f]['label'] if f in FOCUSES else f}"
    if n == "coding_workspace_status":
        return "Checking the computer, GitHub and deploy setup"
    if n == "sandbox_shell":
        return f"Running `{_clip(a.get('command'), 70)}` on the computer"
    # deepagents built-ins
    if n == "write_todos":
        todos = a.get("todos") or []
        current = next((t for t in todos if isinstance(t, dict) and t.get("status") == "in_progress"), None)
        if current is None:
            current = next((t for t in todos if isinstance(t, dict) and t.get("status") != "completed"), None)
        if isinstance(current, dict) and current.get("content"):
            return f"Next: {_clip(current['content'], 64)}"
        return f"Planning {len(todos)} steps" if todos else "Updating the plan"
    if n == "task":
        return f"Starting a helper on: {_clip(a.get('description'), 60)}"
    if n in {"read_file", "write_file", "edit_file"}:
        verb = {"read_file": "Reading", "write_file": "Writing", "edit_file": "Editing"}[n]
        return f"{verb} {_clip(a.get('file_path') or a.get('path'), 50)}"
    if n in {"ls", "glob"}:
        return f"Listing files in {_clip(a.get('path') or a.get('pattern') or '/', 40)}"
    if n == "grep":
        return f"Searching files for {_quote(a.get('pattern'))}"
    k, v = _key_arg(a)
    return f"{humanize_tool(n).capitalize()}" + (f": {_clip(v, 48)}" if v is not None else "")


def _tool_short(name: str, args: dict | None, connector_names: dict[str, str] | None) -> str:
    a = args if isinstance(args, dict) else {}
    if name == "call_tool" and a.get("connector_slug"):
        slug = str(a["connector_slug"])
        return (connector_names or {}).get(slug) or slug
    if name == "search_tools":
        return "the tool search"
    if name == "ask_teammate":
        return "your teammate"
    return humanize_tool(name)


class ProgressReporter:
    """Collects status lines for one chat turn and mirrors them to Redis."""

    def __init__(self, user_id: Any, progress_id: str | None, *, redis_client: Any = None, prefix: str = ""):
        self.user_id = user_id
        self.progress_id = progress_id if valid_progress_id(progress_id) else None
        self.prefix = prefix
        self.steps: list[dict] = []
        self._redis = redis_client
        self._parent: ProgressReporter | None = None

    @property
    def enabled(self) -> bool:
        return self.progress_id is not None

    def child(self, agent_name: str) -> "ProgressReporter":
        """Reporter for a teammate run; lines are prefixed with the teammate's name."""
        kid = ProgressReporter(self.user_id, None, redis_client=self._redis, prefix=f"{agent_name}: ")
        kid._parent = self
        return kid

    def _client(self):
        if self._redis is None:
            from app.cache import get_redis

            self._redis = get_redis()
        return self._redis

    async def emit(self, text: str | None, kind: str = "step", tool: str = "") -> None:
        if not text:
            return
        if self._parent is not None:
            await self._parent.emit(self.prefix + text, kind, tool)
            return
        line = _clip(self.prefix + text, 140)
        if self.steps and self.steps[-1]["text"] == line:
            return
        step = {"text": line, "kind": kind, "tool": tool, "at": round(time.time(), 2)}
        self.steps.append(step)
        if not self.enabled or len(self.steps) > MAX_STEPS:
            return
        try:
            r = self._client()
            key = _key(self.user_id, self.progress_id)
            await r.rpush(key, json.dumps(step))
            await r.expire(key, PROGRESS_TTL_S)
        except Exception as exc:  # noqa: BLE001
            log.debug("progress emit failed: %s", exc)

    async def finish(self) -> None:
        await self.emit("Done", kind="done")


async def read_progress(user_id: Any, progress_id: str, redis_client: Any = None) -> list[dict]:
    if not valid_progress_id(progress_id):
        return []
    if redis_client is None:
        from app.cache import get_redis

        redis_client = get_redis()
    try:
        rows = await redis_client.lrange(_key(user_id, progress_id), 0, -1)
    except Exception as exc:  # noqa: BLE001
        log.debug("progress read failed: %s", exc)
        return []
    out = []
    for raw in rows or []:
        try:
            out.append(json.loads(raw))
        except Exception:  # noqa: BLE001
            continue
    return out


def _parse_inputs(input_str: str, inputs: Any) -> dict:
    if isinstance(inputs, dict):
        return inputs
    try:
        data = json.loads(input_str) if input_str else {}
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _ai_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, list):
        content = " ".join(
            (b.get("text") or "") if isinstance(b, dict) and b.get("type") == "text" else (b if isinstance(b, str) else "")
            for b in content
        )
    return str(content or "").strip()


class ProgressCallback(AsyncCallbackHandler):
    """Turns LangChain model/tool events from the agent graph into status lines."""

    raise_error = False

    def __init__(
        self,
        reporter: ProgressReporter,
        query: str = "",
        connector_names: dict[str, str] | None = None,
        agent_names: dict[str, str] | None = None,
    ):
        self.reporter = reporter
        self.query = query
        self.connector_names = connector_names or {}
        self.agent_names = agent_names or {}
        self.model_calls = 0
        self.last_tool = ""
        self.tools_since_model = 0

    async def _model_start(self) -> None:
        self.model_calls += 1
        if self.model_calls == 1:
            q = _clip(self.query, 60)
            await self.reporter.emit(f"Working out how to handle {_quote(q)}" if q else "Reading your message", kind="model")
        elif self.tools_since_model and self.last_tool:
            await self.reporter.emit(f"Going over what {self.last_tool} returned", kind="model")
        self.tools_since_model = 0

    async def on_chat_model_start(self, serialized, messages, **kwargs) -> None:  # noqa: ANN001
        await self._model_start()

    async def on_llm_start(self, serialized, prompts, **kwargs) -> None:  # noqa: ANN001
        await self._model_start()

    async def on_llm_end(self, response, **kwargs) -> None:  # noqa: ANN001
        try:
            gen = response.generations[-1][-1]
            msg = getattr(gen, "message", None)
        except Exception:  # noqa: BLE001
            return
        if msg is None:
            return
        calls = getattr(msg, "tool_calls", None) or []
        text = _ai_text(msg)
        if calls and text:
            # The model narrated its own next step ("I'll check your calendar…"): show it.
            first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
            await self.reporter.emit(_clip(first, 120), kind="narration")
        elif not calls and text:
            await self.reporter.emit("Writing the reply", kind="reply")

    async def on_tool_start(self, serialized, input_str, *, inputs=None, **kwargs) -> None:  # noqa: ANN001
        name = (serialized or {}).get("name") or kwargs.get("name") or ""
        args = _parse_inputs(input_str, inputs)
        line = describe_tool(name, args, self.connector_names)
        self.last_tool = _tool_short(name, args, self.connector_names)
        if name == "ask_teammate":
            who = self.agent_names.get(str(args.get("agent_id") or "").strip())
            if who:
                line = f"Asking {who}: {_clip(args.get('prompt'), 60)}"
                self.last_tool = who
        self.tools_since_model += 1
        await self.reporter.emit(line, kind="tool", tool=name)

    async def on_tool_error(self, error, **kwargs) -> None:  # noqa: ANN001
        await self.reporter.emit(f"{self.last_tool or 'A tool'} hit an error, trying another way", kind="error")
