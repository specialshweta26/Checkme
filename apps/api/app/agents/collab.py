"""Group chat routing: @mentions, one owner per stage, 2–6 bots."""

from __future__ import annotations

import re
from typing import Any

MENTION_RE = re.compile(r"@([A-Za-z0-9._-]+)")
ROLL_CALL_RE = re.compile(
    r"\b("
    r"each (of (you|us) )?reply"
    r"|introduce yourselves"
    r"|introductions?"
    r"|say hi\b"
    r"|say hello"
    r"|one[- ]line hello"
    r"|what you(?:'re| are) best at"
    r"|who(?:'s| is) (?:here|on this)"
    r"|roll call"
    r")\b",
    re.I,
)
HELLO_RE = re.compile(r"^\s*(hi|hey|hello|yo)(\s+(team|everyone|all))?[!.\s]*$", re.I)
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


def is_roll_call(text: str) -> bool:
    """True when the user wants every bot to speak as itself (hello / introductions)."""
    return bool(ROLL_CALL_RE.search(text or ""))


def is_group_hello(text: str) -> bool:
    """True for a short greeting or an explicit roll-call in a group thread."""
    return is_roll_call(text) or bool(HELLO_RE.match((text or "").strip()))


def is_kickoff(history: list | None) -> bool:
    """True on the first group turn — no assistant messages yet."""
    for item in history or []:
        role = item.get("role") if isinstance(item, dict) else getattr(item, "role", None)
        if role == "assistant":
            return False
    return True


def creator_name(user) -> str:
    raw = (getattr(user, "display_name", None) or "").strip()
    if not raw:
        raw = (getattr(user, "email", None) or "").split("@")[0].strip()
    if not raw:
        return "the user"
    return raw.split()[0]


def role_line(agent) -> str:
    """One roster line from this bot's own profile — never invented brands."""
    from app.agents.focus import focus_label

    name = (getattr(agent, "name", None) or "agent").strip() or "agent"
    title = (getattr(agent, "title", None) or "").strip()
    focus = focus_label(getattr(agent, "focus", None))
    extra = (getattr(agent, "instructions", None) or "").strip().split("\n")[0].strip()[:140]
    role = title or focus
    if extra and role:
        return f"{name}: {role} — {extra}"
    if extra:
        return f"{name}: {extra}"
    if role:
        return f"{name}: {role}"
    return name


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


def routing_prompt(group, owner, teammates: list, mentions: dict | None = None, user=None) -> str:
    mentions = mentions or {}
    peers = [p for p in teammates if str(getattr(p, "id", "")) != str(getattr(owner, "id", ""))]
    roster = ", ".join(f"{getattr(p, 'name', 'agent')} (id={p.id})" for p in peers) or "none"
    outcome = (getattr(group, "outcome", None) or "").strip()
    creator = creator_name(user)
    owner_name = getattr(owner, "name", "the owner")
    group_name = getattr(group, "name", "group")
    roles = "; ".join(role_line(m) for m in teammates) or "none"
    if mentions.get("handoff"):
        return (
            f"You are posting in the group '{group_name}'. {creator} created this group. "
            f"A teammate handed you this in the shared thread. Reply in first person as {owner_name}. "
            "Speak only for yourself from your title, focus, and instructions. "
            "Do not invent teammates' companies, products, or dashboards. "
            "Do not mention tools or that you were asked. Keep it short.\n"
        )
    bits = [
        f"You are working in the group '{group_name}' with teammates: {roster}.",
        f"{creator} created this group.",
        "You own this stage. One owner per stage is the kickoff rule; hand the next step with ask_teammate.",
        "Bots talk to each other in this thread through ask_teammate so the user does not paste notes.",
        "Each ask_teammate reply posts as that bot's own bubble. Never paste or summarize a teammate's reply into yours.",
        "Conversations and learned memory stay separate per bot — do not read a teammate's private memory.",
        "Every bot shares one cloud computer, files, browser, and logins. Each bot has its own screen.",
        "Only one computer-use task may run at a time. If the computer is busy, wait or hand off.",
        f"Proposed roles from each bot's own profile: {roles}. Use only that — never invent brands or data.",
    ]
    if outcome:
        bits.append(f"Group outcome: {outcome}")
    if mentions.get("kickoff"):
        bits.append(
            f"This is the first message in the group. Open with a short first-person intro as {owner_name}: "
            f"greet the team, say {creator} set this group up so you can work together, then list proposed roles "
            "from the roster above. Then call ask_teammate for EACH teammate with a prompt like: "
            "'Reply in this group as yourself in first person. One or two sentences: Hi everyone, your name here, "
            "and what you are best at from your own role. Do not speak for others.' "
            "Skip mail, computer, and connector tools on this intro turn. "
            "Your chat bubble is ONLY your intro."
        )
    elif mentions.get("everyone") or mentions.get("roll_call"):
        bits.append(
            "The user wants each bot to speak. Call ask_teammate for every teammate with a focused prompt in their voice. "
            "Your bubble is only what YOU say. Do not combine their replies."
        )
    elif mentions.get("agents"):
        bits.append(
            "The user @ mentioned you for this stage. Answer as yourself. "
            "If another bot must speak, write a short in-thread handoff (address them by name) and call ask_teammate "
            "so they reply in their own bubble."
        )
    else:
        bits.append(
            "The user wrote normally. If this is yours, answer as yourself. "
            "If a teammate owns it, address them by name in your bubble and call ask_teammate so they reply as themselves."
        )
    bits.append("Do not ask yourself.")
    return " ".join(bits) + "\n"
