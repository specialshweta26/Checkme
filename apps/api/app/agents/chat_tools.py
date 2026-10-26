"""Built-in chat tools that manage Checkme's own workspace agents and groups.

These run inside the deep agent harness (see ``app.agent.deep.harness_tools``)
so a user can say "create a group called software developers with all my
agents" in any agent's chat. They wrap ``app.agents.service`` so the rules
(unique names, 2-10 group members, Chief cannot be deleted) match the sidebar.

Deletes are destructive: the tool returns a confirmation card and only runs
after the user approves it in chat (``app.agent.confirm`` tokens, slug
``WORKSPACE_SLUG``). Unattended runs (scheduled tasks) cannot delete.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.agent.confirm import sign_confirm
from app.agents import service

WORKSPACE_SLUG = "__checkme_workspace__"
DESTRUCTIVE_TOOLS = {"delete_agent", "delete_group"}
WHERE_AGENTS = "the left sidebar under Personal"
WHERE_GROUPS = "the left sidebar under Personal, below the agents"
ALL_WORDS = {"all", "*", "everyone", "all agents", "all my agents", "every agent"}


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split()).casefold()


def _brief_agent(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "title": row.get("title") or "",
        "default": row.get("kind") == "default",
        "archived": bool(row.get("archived")),
    }


def _brief_group(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "members": [{"id": m.get("id"), "name": m.get("name")} for m in row.get("members") or []],
    }


def _limits() -> dict:
    return {"group_min_members": service.GROUP_MEMBER_MIN, "group_max_members": service.GROUP_MEMBER_MAX}


async def _roster(ctx) -> dict:
    return await service.list_workspace(ctx.db, ctx.user)


def _find(rows: list[dict], ref: Any, label: str) -> dict:
    key = str(ref or "").strip()
    if not key:
        raise service.AgentError(f"Say which {label} you mean.")
    for row in rows:
        if str(row.get("id")) == key:
            return row
    want = _norm(key)
    exact = [r for r in rows if _norm(r.get("name")) == want]
    if len(exact) == 1:
        return exact[0]
    loose = [r for r in rows if want and want in _norm(r.get("name"))]
    if len(loose) == 1:
        return loose[0]
    names = ", ".join(str(r.get("name")) for r in rows) or "none yet"
    if len(loose) > 1:
        raise service.AgentError(f"More than one {label} matches '{key}': {', '.join(str(r.get('name')) for r in loose)}.")
    raise service.AgentError(f"No {label} named '{key}'. Existing {label}s: {names}.")


def _wants_all(members: Any) -> bool:
    if isinstance(members, str):
        return _norm(members) in ALL_WORDS
    if isinstance(members, list) and len(members) == 1 and isinstance(members[0], str):
        return _norm(members[0]) in ALL_WORDS
    return False


def _as_list(members: Any) -> list:
    if members is None:
        return []
    if isinstance(members, str):
        return [part.strip() for part in members.split(",") if part.strip()]
    return list(members)


def _pick_agents(agents: list[dict], members: Any, include_all: bool) -> list[dict]:
    live = [a for a in agents if not a.get("archived")]
    if include_all or _wants_all(members):
        picked = list(live)
    else:
        picked = [_find(agents, ref, "agent") for ref in _as_list(members)]
    out: list[dict] = []
    seen: set[str] = set()
    for row in picked:
        if str(row["id"]) in seen:
            continue
        seen.add(str(row["id"]))
        out.append(row)
    return out


def _size_error(count: int, all_agents: bool) -> str | None:
    lo, hi = service.GROUP_MEMBER_MIN, service.GROUP_MEMBER_MAX
    if count < lo:
        hint = " Create another agent first, then add it." if all_agents else ""
        return f"A group needs {lo} to {hi} agents, and this would have {count}.{hint}"
    if count > hi:
        return f"A group can have at most {hi} agents, and this would have {count}. Pick up to {hi} agents for this group."
    return None


def _changed(ctx) -> None:
    ctx.workspace_changed = True


def _error(exc: Exception) -> dict:
    return {"error": str(exc)[:300]}


async def list_agents_and_groups(ctx) -> dict:
    roster = await _roster(ctx)
    return {
        "agents": [_brief_agent(a) for a in roster.get("agents") or []],
        "groups": [_brief_group(g) for g in roster.get("groups") or []],
        "limits": _limits(),
        "where": WHERE_AGENTS,
        "note": "Groups have no separate lead setting; the first member answers and can ask teammates.",
    }


async def create_agent(ctx, name: str, title: str = "", instructions: str = "") -> dict:
    try:
        row = await service.create_agent(ctx.db, ctx.user, {"name": name, "title": title or "", "instructions": instructions or ""})
    except service.AgentError as exc:
        return _error(exc)
    _changed(ctx)
    return {"ok": True, "created": "agent", "agent": _brief_agent(service.serialize_agent(row)), "where": WHERE_AGENTS}


async def update_agent(
    ctx,
    agent: str,
    name: str | None = None,
    title: str | None = None,
    instructions: str | None = None,
    archived: bool | None = None,
) -> dict:
    try:
        roster = await _roster(ctx)
        target = _find(roster.get("agents") or [], agent, "agent")
        body = {"name": name, "title": title, "instructions": instructions, "archived": archived}
        if all(v is None for v in body.values()):
            return {"error": "Nothing to change. Pass name, title, instructions, or archived."}
        row = await service.update_agent(ctx.db, ctx.user, uuid.UUID(str(target["id"])), body)
    except service.AgentError as exc:
        return _error(exc)
    _changed(ctx)
    return {"ok": True, "updated": "agent", "agent": _brief_agent(service.serialize_agent(row)), "where": WHERE_AGENTS}


async def create_group(ctx, name: str, members: Any = None, all_agents: bool = False) -> dict:
    try:
        roster = await _roster(ctx)
        agents = roster.get("agents") or []
        everyone = bool(all_agents) or _wants_all(members)
        picked = _pick_agents(agents, members, everyone)
        problem = _size_error(len(picked), everyone)
        if problem:
            return {"error": problem, "agents": [_brief_agent(a) for a in agents], "limits": _limits()}
        group, rows = await service.create_group(
            ctx.db, ctx.user, {"name": name, "member_ids": [str(a["id"]) for a in picked]}
        )
    except service.AgentError as exc:
        return _error(exc)
    _changed(ctx)
    return {
        "ok": True,
        "created": "group",
        "group": _brief_group(service.serialize_group(group, rows)),
        "where": WHERE_GROUPS,
    }


async def _change_members(ctx, group: str, add: Any = None, remove: Any = None, name: str | None = None) -> dict:
    try:
        roster = await _roster(ctx)
        agents = roster.get("agents") or []
        target = _find(roster.get("groups") or [], group, "group")
        current = [str(m.get("id")) for m in target.get("members") or []]
        ids = list(current)
        for row in _pick_agents(agents, add, False) if add else []:
            if str(row["id"]) not in ids:
                ids.append(str(row["id"]))
        if remove:
            drop = {str(r["id"]) for r in _pick_agents(agents, remove, False)}
            ids = [i for i in ids if i not in drop]
        body: dict = {}
        if name is not None:
            body["name"] = name
        if add or remove:
            problem = _size_error(len(ids), False)
            if problem:
                return {"error": problem, "group": _brief_group(target), "limits": _limits()}
            body["member_ids"] = ids
        if not body:
            return {"error": "Nothing to change."}
        row, members = await service.update_group(ctx.db, ctx.user, uuid.UUID(str(target["id"])), body)
    except service.AgentError as exc:
        return _error(exc)
    _changed(ctx)
    return {"ok": True, "updated": "group", "group": _brief_group(service.serialize_group(row, members)), "where": WHERE_GROUPS}


async def add_group_members(ctx, group: str, members: Any) -> dict:
    return await _change_members(ctx, group, add=members)


async def remove_group_members(ctx, group: str, members: Any) -> dict:
    return await _change_members(ctx, group, remove=members)


async def rename_group(ctx, group: str, name: str) -> dict:
    return await _change_members(ctx, group, name=name)


def _already_confirmed(ctx, tool: str, target_id: str) -> bool:
    return any(
        c.get("slug") == WORKSPACE_SLUG and c.get("tool") == tool and str((c.get("args") or {}).get("id")) == target_id
        for c in ctx.confirmations or []
    )


async def request_delete(ctx, kind: str, ref: str) -> dict:
    """Queue a confirmation card for deleting an agent or group. Never deletes directly."""
    tool = "delete_agent" if kind == "agent" else "delete_group"
    try:
        roster = await _roster(ctx)
        rows = roster.get("agents" if kind == "agent" else "groups") or []
        target = _find(rows, ref, kind)
    except service.AgentError as exc:
        return _error(exc)
    if kind == "agent" and target.get("kind") == "default":
        return {"error": "The default agent cannot be deleted."}
    target_id = str(target["id"])
    if _already_confirmed(ctx, tool, target_id):
        return {"status": "already_confirmed", "message": f"The user already confirmed deleting {target['name']} this turn."}
    if ctx.auto_confirm:
        return {"error": f"Deleting a {kind} needs the user's approval in chat, so it cannot run from a scheduled task."}
    args = {"id": target_id, "name": target.get("name")}
    if kind == "agent":
        detail = "Deletes this agent with its memory, scheduled tasks and automations. Its old chats are kept."
    else:
        detail = "Deletes this group. Its member agents are kept."
    token = sign_confirm("tool", {"slug": WORKSPACE_SLUG, "tool": tool, "args": args})
    preview = {"text": f"Delete {kind} '{target.get('name')}'. {detail}"}
    ctx.pending.append({"needs_confirm": True, "token": token, "title": f"Delete {kind}: {target.get('name')}", "preview": preview})
    return {"status": "awaiting_user_confirmation", "preview": preview}


async def run_confirmed(ctx, tool: str, args: dict) -> dict:
    """Run a delete the user approved on the confirmation card."""
    try:
        target = uuid.UUID(str((args or {}).get("id")))
    except (TypeError, ValueError):
        return {"error": "That confirmation is not valid any more."}
    name = (args or {}).get("name") or "it"
    try:
        if tool == "delete_agent":
            await service.delete_agent(ctx.db, ctx.user, target)
        elif tool == "delete_group":
            await service.delete_group(ctx.db, ctx.user, target)
        else:
            return {"error": f"unknown workspace action {tool}"}
    except service.AgentError as exc:
        return _error(exc)
    _changed(ctx)
    return {"ok": True, "deleted": tool.removeprefix("delete_"), "name": name}
