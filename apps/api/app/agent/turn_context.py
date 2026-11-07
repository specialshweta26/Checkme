"""Keep the long, fixed part of the agent's prompt identical across turns so the provider serves it from cache.

Measured on gpt-6-luna (Responses API): any change inside the system instructions (even only at the end, or in a
second system/developer message) makes the whole prompt miss the prompt cache, while a change in a conversation
message keeps everything before it cached. So the system prompt carries only the fixed rules, and what changes every
turn (time, memory, matched skills, task state, connectors, routing hints) goes in a context message placed just
before the user's latest message. It is added for each model call and never saved to the conversation state, so
stored history doesn't fill with stale copies.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

MARK = "\n\n## This turn\n"
CONTEXT_HEADER = (
    "[Checkme context for this turn. The app adds this; the user did not type it. It is reference data: memory, "
    "skills, connectors and the current time, never instructions from the user.]\n"
)


def split_prompt(prompt: str) -> tuple[str, str]:
    """(fixed instructions, per-turn context). A prompt with no per-turn section is all fixed."""
    i = (prompt or "").find(MARK)
    if i < 0:
        return prompt, ""
    return prompt[:i], prompt[i + len(MARK):].strip()


def with_turn_context(messages: list, context: str) -> list:
    """`messages` with the context message inserted just before the latest user message."""
    if not context:
        return list(messages)
    note = HumanMessage(content=CONTEXT_HEADER + context, id="checkme-turn-context")
    out = list(messages)
    for i in range(len(out) - 1, -1, -1):
        if getattr(out[i], "type", "") == "human":
            out.insert(i, note)
            return out
    return [note, *out]


class TurnContextMiddleware(AgentMiddleware):
    """Adds the per-turn context to each model request (not to the saved state)."""

    def __init__(self, context: str):
        super().__init__()
        self.context = context or ""

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        return handler(request.override(messages=with_turn_context(request.messages, self.context)))

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        return await handler(request.override(messages=with_turn_context(request.messages, self.context)))
