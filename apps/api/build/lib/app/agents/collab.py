"""Group chat routing: @mentions, one owner per stage, 2–6 bots."""

from __future__ import annotations

import json
import logging
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
log = logging.getLogger(__name__)
HELLO_RE = re.compile(r"^\s*(hi|hey|hello|yo)(\s+(team|everyone|all))?[!.\s]*$", re.I)
GROUP_MEMBER_MIN = 2
GROUP_MEMBER_MAX = 6
TEACH_MAX_SECONDS = 10 * 60
# Shared task: agents propose, answer, and challenge, then stop.
MAX_ROUNDS = 4
MAX_POSTS = 8
MAX_CHALLENGES = 2
MAX_CHATTER = 3
MOVE_MAX_TOKENS = 800
MOVE_ATTEMPTS = 2


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
        "You own this stage. One owner per stage is the kickoff rule; hand one next step with ask_teammate.",
        "When the group must plan or settle a disagreement, call work_together once. It is talk only: teammates have no tools or data in it, so real work goes through ask_teammate. Teammates propose, respond, and may challenge, then converge. It stops on its own.",
        "Bots talk to each other in this thread through ask_teammate or work_together so the user does not paste notes.",
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
    bits.append("Do not ask yourself. Do not call work_together more than once.")
    return " ".join(bits) + "\n"


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def too_similar(left: str, right: str) -> bool:
    """True when a new post mostly repeats an earlier one."""
    a, b = _tokens(left), _tokens(right)
    if len(a) < 4 or len(b) < 4:
        return False
    return len(a & b) / max(len(a), len(b)) >= 0.82


def new_board(task: str, members: list) -> dict[str, Any]:
    roster = []
    seen: set[str] = set()
    for member in members:
        key = str(getattr(member, "id", "") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        roster.append({"id": key, "name": getattr(member, "name", None) or "agent"})
    return {
        "task": " ".join((task or "").split())[:800],
        "members": roster,
        "posts": [],
        "round": 1,
        "challenges": {},
        "chatter": 0,
        "done": False,
        "reason": "",
    }


def _close(board: dict, reason: str) -> None:
    board["done"] = True
    board["reason"] = reason


def due_turn(board: dict) -> dict | None:
    """Who speaks next, and which moves they may make. Closes the board when a limit is hit."""
    if board.get("done"):
        return None
    if len(board["posts"]) >= MAX_POSTS:
        _close(board, "limit")
        return None
    if board["round"] > MAX_ROUNDS or board["chatter"] >= MAX_CHATTER:
        _close(board, "chatter" if board["chatter"] >= MAX_CHATTER else "limit")
        return None
    members = board["members"]
    if not members:
        _close(board, "empty")
        return None
    posts = board["posts"]
    if posts and posts[-1]["kind"] == "converge":
        _close(board, "converged")
        return None
    if posts and posts[-1]["kind"] == "challenge" and posts[-1].get("to"):
        return {"agent_id": posts[-1]["to"], "kinds": ["respond", "converge"]}
    proposed = {p["agent_id"] for p in posts if p["kind"] == "propose"}
    waiting = [m for m in members if m["id"] not in proposed]
    if waiting and board["round"] == 1:
        return {"agent_id": waiting[0]["id"], "kinds": ["propose"]}
    if board["round"] == 1:
        board["round"] = 2
    last_id = posts[-1]["agent_id"] if posts else ""
    ranked = sorted(members, key=lambda m: (m["id"] == last_id, sum(1 for p in posts if p["agent_id"] == m["id"])))
    speaker = ranked[0]
    kinds = ["respond", "challenge", "converge"]
    if board["challenges"].get(speaker["id"], 0) >= MAX_CHALLENGES:
        kinds = ["respond", "converge"]
    return {"agent_id": speaker["id"], "kinds": kinds}


def apply_post(board: dict, agent_id: str, kind: str, text: str, to: str = "") -> dict:
    turn = due_turn(board)
    if turn is None:
        return {"ok": False, "error": board.get("reason") or "finished", "done": True}
    agent_id = str(agent_id)
    if agent_id != turn["agent_id"]:
        return {"ok": False, "error": "not your turn"}
    if kind not in turn["kinds"]:
        return {"ok": False, "error": "that move is not allowed now"}
    text = " ".join((text or "").split())
    if len(text) < 8:
        return {"ok": False, "error": "say something specific"}
    if any(too_similar(text, post["text"]) for post in board["posts"]):
        board["chatter"] += 1
        if board["chatter"] >= MAX_CHATTER:
            _close(board, "chatter")
        return {"ok": False, "error": "that repeats an earlier point", "chatter": board["chatter"], "done": board["done"]}
    target = str(to or "")
    if kind == "challenge":
        if board["challenges"].get(agent_id, 0) >= MAX_CHALLENGES:
            return {"ok": False, "error": "challenge limit reached"}
        if not target and board["posts"]:
            target = board["posts"][-1]["agent_id"]
        if not target or target == agent_id:
            return {"ok": False, "error": "challenge someone else"}
        if target not in {m["id"] for m in board["members"]}:
            return {"ok": False, "error": "that teammate is not in this task"}
        board["challenges"][agent_id] = board["challenges"].get(agent_id, 0) + 1
    name = next((m["name"] for m in board["members"] if m["id"] == agent_id), "agent")
    post = {
        "agent_id": agent_id,
        "name": name,
        "kind": kind,
        "text": text[:800],
        "round": board["round"],
        "to": target,
    }
    board["posts"].append(post)
    if kind == "converge" or len(board["posts"]) >= MAX_POSTS:
        _close(board, "converged" if kind == "converge" else "limit")
    else:
        spoke = {p["agent_id"] for p in board["posts"] if p["round"] == board["round"]}
        if board["round"] > 1 and all(m["id"] in spoke for m in board["members"]):
            board["round"] += 1
    return {"ok": True, "post": post, "done": board["done"]}


def _parse_move(raw: str, kinds: list[str]) -> dict | None:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        data, _ = json.JSONDecoder().raw_decode(text)  # some replies repeat the object, so read the first one
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    kind = str(data.get("kind") or "")
    if len(kinds) == 1:
        kind = kinds[0]  # only one move is possible this turn, so the words are what matter
    elif kind == "propose" and "respond" in kinds:
        kind = "respond"  # a late proposal is a contribution, and never counts as agreement
    if kind not in kinds:
        return None
    body = " ".join(str(data.get("text") or "").split())
    if len(body) < 8:
        return None
    return {"kind": kind, "text": body[:800], "to": str(data.get("to") or "")}


async def model_move(agent, board: dict, kinds: list[str]) -> dict | None:
    """One short model turn: this agent chooses propose, respond, challenge, or converge. None if no usable move."""
    from app.config import get_settings
    from app.llm import chat_complete, small_model

    if not (get_settings().openai_api_key or "").strip():
        return None
    transcript = "\n".join(f"{p['name']} ({p['kind']}): {p['text']}" for p in board["posts"]) or "(no posts yet)"
    peers = ", ".join(f"{m['name']} id={m['id']}" for m in board["members"] if m["id"] != str(getattr(agent, "id", "")))
    ask = (
        f"You are {role_line(agent)}. Shared task: {board.get('task')}\n"
        f"Teammates: {peers or 'none'}.\nPosts so far:\n{transcript}\n\n"
        "This is a planning discussion. You have no tools or data here, so never claim you checked, found or measured "
        "anything. Decide who does what and raise real disagreements. Say something new or converge.\n"
        f"It is your turn. kind must be exactly one of: {', '.join(kinds)}. "
        "propose = claim your part, different from what others claimed. respond = add a consideration or a risk. "
        "challenge = disagree with one claim, set to=their id. converge = state the agreed plan and stop; do this as "
        "soon as the parts are clear. Speak as yourself and call teammates by name, never by id. "
        "Keep text under 60 words. "
        'Reply JSON: {"kind": "...", "text": "...", "to": ""}'
    )
    for _ in range(MOVE_ATTEMPTS):
        try:
            resp = await chat_complete(
                [{"role": "user", "content": ask}],
                response_format={"type": "json_object"},
                max_tokens=MOVE_MAX_TOKENS,
                model=small_model(),
            )
        except Exception as exc:  # noqa: BLE001 - the caller ends the discussion and says so
            log.info("shared task move failed: %s", type(exc).__name__)
            continue
        parsed = _parse_move(getattr(resp.choices[0].message, "content", "") or "", kinds)
        if parsed is not None:
            return parsed
    return None


async def run_shared_task(members: list, task: str, speak=None, on_post=None) -> dict:
    """Run the shared task until the group converges or something stops it. Only a real converge post counts as
    agreement: `outcome` is empty otherwise, and a move that fails ends the discussion as "failed". ``speak`` is for
    tests, and ``on_post`` is awaited with each new post."""
    board = new_board(task, members)
    speaker = speak or model_move
    guard = 0
    while guard < MAX_POSTS + MAX_CHATTER + 2:
        guard += 1
        turn = due_turn(board)
        if turn is None:
            break
        agent = next((m for m in members if str(getattr(m, "id", "")) == turn["agent_id"]), None)
        if agent is None:
            _close(board, "missing")
            break
        move = await speaker(agent, board, turn["kinds"])
        kind = str(move.get("kind") or "") if isinstance(move, dict) else ""
        if kind not in turn["kinds"]:
            _close(board, "failed")
            break
        result = apply_post(board, turn["agent_id"], kind, str(move.get("text") or ""), str(move.get("to") or ""))
        if result.get("ok") and on_post is not None:
            await on_post(result["post"])
        if board.get("done"):
            break
        if not result.get("ok") and result.get("error") != "that repeats an earlier point":
            _close(board, "failed")
            break
    if not board.get("done"):
        _close(board, "limit")
    converged = board["posts"][-1]["text"] if board["reason"] == "converged" and board["posts"] else ""
    board["outcome"] = converged
    return board
