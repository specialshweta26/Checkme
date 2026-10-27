"""Group chat routing: @mentions, one owner per stage, 2–6 bots."""

from __future__ import annotations

import re
from typing import Any

MENTION_RE = re.compile(r"@([A-Za-z0-9._-]+)")
GROUP_MEMBER_MIN = 2
GROUP_MEMBER_MAX = 6
TEACH_MAX_SECONDS = 10 * 60


def parse_mentions(text: str, members: list) -> dict[str, Any]:
    """Find @Name and @everyone in a user message."""
    names = {str(getattr(m, "name", "")).strip().lower(): m for m in members if getattr(m, "name", None)}
    everyone = False
    hits: list = []
    seen: set[str] = set()
    for match in MENTION_RE.finditer(text or ""):
        token = match.group(1).lower()
        if token == "everyone":
            everyone = True
            continue
        hit = names.get(token)
        if hit is None:
            hit = next((m for n, m in names.items() if n.startswith(token) or token.startswith(n)), None)
        if hit is None:
            continue
        key = str(getattr(hit, "id", ""))
        if key in seen:
            continue
        seen.add(key)
        hits.append(hit)
    return {"everyone": everyone, "agents": hits}


def pick_owner(members: list, group=None, session_state: dict | None = None, mentions: dict | None = None):
    """One owner per stage. @Name wins; otherwise the group's kickoff owner."""
    mentions = mentions or {}
    named = mentions.get("agents") or []
    if named and not mentions.get("everyone"):
        return named[0]
    owner_id = None
    if isinstance(session_state, dict):
        owner_id = session_state.get("owner_id")
    if not owner_id:
        owner_id = getattr(group, "owner_id", None)
    if owner_id:
        hit = next((m for m in members if str(getattr(m, "id", "")) == str(owner_id)), None)
        if hit is not None:
            return hit
    return members[0] if members else None


def routing_prompt(group, owner, teammates: list, mentions: dict | None = None) -> str:
    mentions = mentions or {}
    peers = [p for p in teammates if str(getattr(p, "id", "")) != str(getattr(owner, "id", ""))]
    roster = ", ".join(f"{getattr(p, 'name', 'agent')} (id={p.id})" for p in peers) or "none"
    outcome = (getattr(group, "outcome", None) or "").strip()
    bits = [
        f"You are working in the group '{getattr(group, 'name', 'group')}' with teammates: {roster}.",
        "You own this stage. One owner per stage is the kickoff rule; hand the next step with ask_teammate.",
        "Bots message each other through ask_teammate so the user does not paste notes.",
        "Conversations and learned memory stay separate per bot — do not read a teammate's private memory.",
        "Every bot shares one cloud computer, files, browser, and logins. Each bot has its own screen.",
        "Only one computer-use task may run at a time. If the computer is busy, wait or hand off.",
    ]
    if outcome:
        bits.append(f"Group outcome: {outcome}")
    if mentions.get("everyone"):
        bits.append(
            "The user wrote @everyone. Call ask_teammate for each teammate with a focused update, then combine replies."
        )
    elif mentions.get("agents"):
        bits.append("The user @ mentioned you for this stage. Do the work; hand off only if another bot must own the next step.")
    else:
        bits.append("The user wrote normally. Decide who should act. Use ask_teammate to split independent work.")
    bits.append("Do not ask yourself. Combine teammate replies into the user's answer.")
    return " ".join(bits) + "\n"
