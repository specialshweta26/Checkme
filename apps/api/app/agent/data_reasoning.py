"""More reasoning once a turn is working with a connector's data (docs/LLD.md "Data turns").

The agent runs with reasoning off (REASONING_EFFORT, usually "none") so quick answers stay quick. On a data question
that meant it took the first tool result as final: a tool that ignored the date filter gave "can't verify", where a
reasoning model tries another tool, notices the filter was ignored, and reads the whole table instead. So the turn
starts as before, and once it has used a data tool (tool search, call_tool, a stored result) its next model calls get
DATA_REASONING_EFFORT (default "medium"). Quick questions never reach a data tool and are unchanged.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

DATA_TOOLS = {"search_tools", "call_tool", "get_tool_schema", "inspect_result", "aggregate_result", "table_to_computer"}


def effort() -> str:
    from app.config import get_settings

    return str(getattr(get_settings(), "data_reasoning_effort", "medium") or "").strip().lower()


def doing_data_work(messages: list) -> bool:
    for m in messages:
        for call in getattr(m, "tool_calls", None) or []:
            if str(call.get("name") or "") in DATA_TOOLS:
                return True
    return False


def settings_for(model_name: str, messages: list) -> dict | None:
    """The per-call ``reasoning`` setting for this model call, or None to leave it as configured."""
    from app import llm_gateway

    want = effort()
    if not want or want == "none" or not doing_data_work(messages):
        return None
    current = llm_gateway.effort_for(model_name)
    if current == llm_gateway.OMIT_EFFORT or llm_gateway.learned_effort(model_name):
        return None  # the model takes no reasoning setting, or refused one before: keep what it accepts
    if current not in ("none", "minimal", "low", ""):
        return None  # already reasoning at least this much
    return {"reasoning": {"effort": want, "summary": "auto"}}


def middleware(model_name: str, is_openai: bool, picked: str = ""):
    """``picked``: a reasoning level the user chose for this model; their choice is kept as it is."""
    from langchain.agents.middleware import AgentMiddleware

    class DataReasoning(AgentMiddleware):
        def _request(self, request):  # noqa: ANN001
            if not is_openai or picked:
                return request
            extra = settings_for(model_name, list(request.messages))
            if not extra:
                return request
            return request.override(model_settings={**(request.model_settings or {}), **extra})

        def wrap_model_call(self, request, handler):  # noqa: ANN001
            return handler(self._request(request))

        async def awrap_model_call(self, request, handler):  # noqa: ANN001
            return await handler(self._request(request))

    return DataReasoning()
