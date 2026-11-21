"""A specialist agent keeps to its role (docs/GUARDRAILS.md "Roles").

The role rule in the prompt alone wasn't enough: asked for sales figures, the HR agent fetched them. Before a
specialist's chat turn starts, one small model call compares the request with the agent's role and instructions. A
request clearly outside them is answered at once, without tools, naming the agent whose role fits; when the user then
says to go ahead (or asks again insisting), the agent does it. The all-round agent (Chief, or one without a role of
its own) is never checked, nor are background jobs, teammate hand-offs or replies to an approval. Any failure lets the
request through: the check must never block the user.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.agents.focus import is_general

log = logging.getLogger(__name__)

_SMALL_TALK = re.compile(r"^\s*(hi|hello|hey|thanks|thank you|ok|okay|yes|no|go ahead|do it|sure|bye|good (morning|evening|night))\b", re.I)

PROMPT = (
    "Decide whether a request fits an assistant's role. Answer with JSON only: "
    '{{"in_role": true|false, "fits": "<name of the agent whose role fits, from the list, or empty>"}}.\n'
    "The request is in role when the role or instructions plausibly cover it, when it is small talk or a question about "
    "the assistant itself, when it continues the conversation's current work, or when the user, told it was outside the "
    "role, insists or says to go ahead. Only a request clearly belonging to a different kind of work is out of role.\n\n"
    "Assistant: {name}\nRole: {title}\nInstructions: {instructions}\n\nOther agents:\n{others}\n\n"
    "Recent conversation:\n{history}\n\nRequest: {query}"
)


def _role_text(agent) -> str:
    title = str(getattr(agent, "title", "") or "").strip() or "specialist"
    return title


def _others(agents: list) -> str:
    rows = []
    for a in agents[:30]:
        title = str(getattr(a, "title", "") or "").strip() or ("all-round assistant" if is_general(a) else "assistant")
        instr = re.sub(r"\s+", " ", str(getattr(a, "instructions", "") or ""))[:120]
        rows.append(f"- {getattr(a, 'name', '')}: {title}" + (f" ({instr})" if instr else ""))
    return "\n".join(rows) or "- none"


def _history(history: list[dict]) -> str:
    lines = []
    for m in (history or [])[-4:]:
        role = str(m.get("role") or "")
        if role in ("user", "assistant"):
            lines.append(f"{role}: {re.sub(r'\s+', ' ', str(m.get('content') or ''))[:300]}")
    return "\n".join(lines) or "(none)"


def parse(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("in_role"), bool):
        return None
    return {"in_role": data["in_role"], "fits": str(data.get("fits") or "").strip()}


def reply(agent, fits: str, others: list) -> str:
    name = getattr(agent, "name", "") or "I"
    title = _role_text(agent)
    names = {str(getattr(a, "name", "")).lower(): str(getattr(a, "name", "")) for a in others}
    target = names.get(fits.lower()) if fits else None
    if target is None:
        target = next((str(getattr(a, "name", "")) for a in others if is_general(a)), "")
    where = f"**{target}** is the right agent for it — open {target} and ask there, or" if target else "Ask an agent whose role covers it, or"
    return (
        f"That's outside my role as {name}, your {title}. {where} say **go ahead** and I'll do it here anyway."
    )


async def check(agent, query: str, history: list[dict], others: list, complete=None) -> dict | None:
    """``{"text", "fits"}`` when the request is clearly outside a specialist's role; None to go on."""
    if agent is None or is_general(agent) or not (query or "").strip() or _SMALL_TALK.match(query or ""):
        return None
    if complete is None:
        from app.config import get_settings
        from app.llm import chat_complete

        small = (getattr(get_settings(), "openai_small_model", "") or "").strip() or None

        async def complete(messages, max_tokens=0):  # noqa: ANN001
            return await chat_complete(messages, max_tokens=max_tokens, model=small)

    others = [a for a in others if str(getattr(a, "id", "")) != str(getattr(agent, "id", ""))]
    prompt = PROMPT.format(
        name=getattr(agent, "name", ""), title=_role_text(agent),
        instructions=re.sub(r"\s+", " ", str(getattr(agent, "instructions", "") or ""))[:800] or "(none)",
        others=_others(others), history=_history(history), query=str(query)[:1500],
    )  # fmt: skip
    try:
        resp = await complete([{"role": "user", "content": prompt}], max_tokens=200)
        verdict = parse(resp.choices[0].message.content or "")
    except Exception:  # noqa: BLE001 - the check never blocks the user
        log.info("role check skipped", exc_info=True)
        return None
    if verdict is None or verdict["in_role"]:
        return None
    return {"text": reply(agent, verdict["fits"], others), "fits": verdict["fits"]}
