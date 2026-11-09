"""Keep the model's context small on long browser runs (docs/LLD.md "Context trimming").

Every browser tool returns the whole page again (text, element tree, tables: up to 60,000 characters). On a long
form-filling run those copies piled up in the conversation, so each model call carried 100,000+ tokens and got slow
enough to look stuck. Only the latest page matters for the next step, so before each model call:

- page results (browser / computer tools) other than the latest one shrink to a short note: address, title, whether it
  worked, errors, warnings, the result or what a task returned;
- any other tool result older than the last few messages and longer than OLD_RESULT_CHARS is cut to that size.

Only what is sent to the model changes; the stored conversation keeps everything (inspect_result can reread it).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

log = logging.getLogger(__name__)

PAGE_TOOLS = {
    "browser_navigate", "browser_snapshot", "browser_act", "browser_task", "browser_replay", "browser_collect",
    "browser_new_tab", "browser_switch_tab", "browser_close_tab", "browser_search", "browser_rank_links", "browser_handoff",
    "browser_download", "computer_observe", "computer_act", "browse_open", "browse_act", "browse_screenshot",
}  # fmt: skip
KEEP_KEYS = ("ok", "url", "title", "error", "warnings", "result", "done", "need_user", "stopped_before", "needs_approval",
             "via", "completed", "deep_link_failed", "fallback_url", "bot_wall", "status", "took_ms")  # fmt: skip
OLD_RESULT_CHARS = 4000
RECENT_MESSAGES = 4
MODEL_CALL_LIMIT_S = 150
MODEL_CALL_TRIES = 2


def _brief(content: str) -> str:
    """A short note for an old page result."""
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        return "[older page result, trimmed] " + str(content)[:400]
    if not isinstance(data, dict):
        return "[older page result, trimmed] " + str(content)[:400]
    page = data.get("page") if isinstance(data.get("page"), dict) else {}
    out = {k: data[k] for k in KEEP_KEYS if k in data and data[k] not in (None, "", [], {})}
    for k in ("url", "title"):
        if k not in out and page.get(k):
            out[k] = page[k]
    downloads = data.get("downloads") or []
    if downloads:
        out["downloads"] = [d.get("filename") for d in downloads if isinstance(d, dict)][:5]
    if isinstance(out.get("result"), str):
        out["result"] = out["result"][:600]
    return "[older page result, trimmed to save context; the latest page is below] " + json.dumps(out, ensure_ascii=False, default=str)[:900]


def _text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    return None


def trim_messages(messages: list) -> tuple[list, int]:
    """(messages for the model, characters saved). Messages are copied, never changed in place."""
    from langchain_core.messages import ToolMessage

    last_page = max((i for i, m in enumerate(messages) if isinstance(m, ToolMessage) and getattr(m, "name", "") in PAGE_TOOLS), default=-1)
    recent_from = len(messages) - RECENT_MESSAGES
    out, saved = [], 0
    for i, m in enumerate(messages):
        text = _text(getattr(m, "content", None)) if isinstance(m, ToolMessage) else None
        if text is None:
            out.append(m)
            continue
        new = text
        if getattr(m, "name", "") in PAGE_TOOLS and i < last_page and len(text) > 1200:
            new = _brief(text)
        elif i < recent_from and len(text) > OLD_RESULT_CHARS:
            new = text[:OLD_RESULT_CHARS] + f"\n…[older result trimmed: {len(text) - OLD_RESULT_CHARS} more characters; inspect_result can reread it]"
        if new is not text:
            saved += len(text) - len(new)
            m = m.model_copy(update={"content": new})
        out.append(m)
    return out, saved


def middleware():
    """The AgentMiddleware that trims each model call's messages, or None if middleware isn't available."""
    try:
        from langchain.agents.middleware import AgentMiddleware
    except Exception:  # noqa: BLE001
        return None

    class TrimOldPages(AgentMiddleware):
        def wrap_model_call(self, request, handler):  # noqa: ANN001
            messages, saved = trim_messages(list(request.messages))
            return handler(request.override(messages=messages) if saved else request)

        async def awrap_model_call(self, request, handler):  # noqa: ANN001
            messages, saved = trim_messages(list(request.messages))
            if saved:
                log.info("context trimmed: %d characters of old tool results", saved)
                request = request.override(messages=messages)
            return await call_with_limit(handler, request)

    return TrimOldPages()


async def call_with_limit(handler, request):  # noqa: ANN001
    """A model call that has stalled gets one more try, so it can't freeze the turn. The client's own timeout covers only
    the gap between pieces of a reply, and turns have sat for minutes with nothing happening."""
    for attempt in range(MODEL_CALL_TRIES):
        try:
            return await asyncio.wait_for(handler(request), MODEL_CALL_LIMIT_S)
        except asyncio.TimeoutError:
            log.warning("model call stalled for %ss (try %s of %s)", MODEL_CALL_LIMIT_S, attempt + 1, MODEL_CALL_TRIES)
    raise TimeoutError("The AI model didn't answer in time.")
