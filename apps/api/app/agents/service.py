from __future__ import annotations

import uuid

from sqlalchemy import delete as sql_delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.collab import GROUP_MEMBER_MAX, GROUP_MEMBER_MIN
from app.models import (
    AgentConnector,
    AgentGroup,
    AgentGroupMember,
    ChatMessage,
    ChatSession,
    Connector,
    User,
    WorkspaceAgent,
)

DEFAULT_AGENT_NAME = "Chief"
DEFAULT_AGENT_TITLE = "chief of staff"
DEFAULT_AGENT_COLOR = "#f97316"
AGENT_COLORS = ("#f97316", "#2f6bff", "#10b981", "#8b5cf6", "#ec4899", "#eab308")
NAME_MAX = 80
INSTRUCTIONS_MAX = 8000
OUTCOME_MAX = 400


class AgentError(ValueError):
    pass


def pick_color(index: int) -> str:
    return AGENT_COLORS[index % len(AGENT_COLORS)]


def serialize_agent(row: WorkspaceAgent) -> dict:
    return {
        "id": str(row.id),
        "name": row.name,
        "title": row.title or "",
        "instructions": row.instructions or "",
        "color": row.color or DEFAULT_AGENT_COLOR,
        "kind": row.kind or "custom",
        "pinned": bool(getattr(row, "pinned", False)),
        "archived": bool(getattr(row, "archived", False)),
        "unread": bool(getattr(row, "unread", False)),
    }


def serialize_group(row: AgentGroup, members: list[WorkspaceAgent]) -> dict:
    owner_id = str(row.owner_id) if getattr(row, "owner_id", None) else None
    return {
        "id": str(row.id),
        "name": row.name,
        "outcome": getattr(row, "outcome", "") or "",
        "owner_id": owner_id,
        "members": [serialize_agent(m) for m in members],
    }


def session_belongs(sess, agent_id=None, group_id=None) -> bool:
    """True when this chat thread is owned by the selected agent or group."""
    if group_id:
        return str(getattr(sess, "group_id", "") or "") == str(group_id)
    if getattr(sess, "group_id", None):
        return False
    want = str(agent_id or "")
    if not want:
        return False
    return str(getattr(sess, "agent_id", "") or "") == want


def _with_thread(payload: dict, sess: ChatSession | None) -> dict:
    if sess is None:
        return payload
    payload["session_id"] = str(sess.id)
    payload["preview"] = (sess.title or "")[:120]
    payload["updated_at"] = sess.updated_at.isoformat() if sess.updated_at else ""
    return payload


def validate_group_members(ids: list) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    seen: set[str] = set()
    for raw in ids or []:
        try:
            uid = uuid.UUID(str(raw))
        except (TypeError, ValueError) as exc:
            raise AgentError("Each group member must be a valid agent id.") from exc
        key = str(uid)
        if key in seen:
            continue
        seen.add(key)
        out.append(uid)
    if len(out) < GROUP_MEMBER_MIN:
        raise AgentError(f"A group needs {GROUP_MEMBER_MIN} to {GROUP_MEMBER_MAX} agents.")
    if len(out) > GROUP_MEMBER_MAX:
        raise AgentError(f"A group needs {GROUP_MEMBER_MIN} to {GROUP_MEMBER_MAX} agents.")
    return out


def _clean_name(name: str) -> str:
    value = " ".join((name or "").split())
    if not value:
        raise AgentError("Name is required.")
    if len(value) > NAME_MAX:
        raise AgentError(f"Name must be {NAME_MAX} characters or fewer.")
    return value


def _clean_instructions(text: str) -> str:
    value = (text or "").strip()
    if len(value) > INSTRUCTIONS_MAX:
        raise AgentError(f"Instructions must be {INSTRUCTIONS_MAX} characters or fewer.")
    return value


def _clean_outcome(text: str) -> str:
    value = " ".join((text or "").split())
    if len(value) > OUTCOME_MAX:
        raise AgentError(f"Outcome must be {OUTCOME_MAX} characters or fewer.")
    return value


def _owner_from_body(body: dict, members: list[WorkspaceAgent]) -> uuid.UUID | None:
    raw = body.get("owner_id")
    if not raw:
        return members[0].id if members else None
    try:
        uid = uuid.UUID(str(raw))
    except (TypeError, ValueError) as exc:
        raise AgentError("Kickoff owner must be a group member.") from exc
    if not any(m.id == uid for m in members):
        raise AgentError("Kickoff owner must be a group member.")
    return uid


async def ensure_default_agent(db: AsyncSession, user: User) -> WorkspaceAgent:
    row = (
        await db.execute(
            select(WorkspaceAgent).where(WorkspaceAgent.user_id == user.id, WorkspaceAgent.kind == "default")
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    named = (
        await db.execute(
            select(WorkspaceAgent).where(WorkspaceAgent.user_id == user.id, WorkspaceAgent.name == DEFAULT_AGENT_NAME)
        )
    ).scalar_one_or_none()
    if named is not None:
        named.kind = "default"
        await db.flush()
        return named
    row = WorkspaceAgent(
        user_id=user.id,
        name=DEFAULT_AGENT_NAME,
        title=DEFAULT_AGENT_TITLE,
        instructions="",
        color=DEFAULT_AGENT_COLOR,
        kind="default",
    )
    db.add(row)
    await db.flush()
    return row


async def list_workspace(db: AsyncSession, user: User) -> dict:
    chief = await ensure_default_agent(db, user)
    agents = (
        (
            await db.execute(
                select(WorkspaceAgent)
            .where(WorkspaceAgent.user_id == user.id)
            .order_by(WorkspaceAgent.pinned.desc(), WorkspaceAgent.archived.asc(), WorkspaceAgent.created_at)
            )
        )
        .scalars()
        .all()
    )
    groups = (
        (await db.execute(select(AgentGroup).where(AgentGroup.user_id == user.id).order_by(AgentGroup.created_at)))
        .scalars()
        .all()
    )
    member_rows = []
    if groups:
        member_rows = (
            (
                await db.execute(
                    select(AgentGroupMember).where(AgentGroupMember.group_id.in_([g.id for g in groups]))
                )
            )
            .scalars()
            .all()
        )
    by_id = {a.id: a for a in agents}
    grouped: dict[uuid.UUID, list[WorkspaceAgent]] = {g.id: [] for g in groups}
    for link in member_rows:
        agent = by_id.get(link.agent_id)
        if agent is not None:
            grouped[link.group_id].append(agent)
    await db.execute(
        update(ChatSession)
        .where(
            ChatSession.user_id == user.id,
            ChatSession.agent_id.is_(None),
            ChatSession.group_id.is_(None),
        )
        .values(agent_id=chief.id)
    )
    threads = (
        (
            await db.execute(
                select(ChatSession).where(ChatSession.user_id == user.id).order_by(ChatSession.updated_at.desc())
            )
        )
        .scalars()
        .all()
    )
    latest_agent: dict[str, ChatSession] = {}
    latest_group: dict[str, ChatSession] = {}
    for sess in threads:
        if sess.group_id:
            latest_group.setdefault(str(sess.group_id), sess)
        elif sess.agent_id:
            latest_agent.setdefault(str(sess.agent_id), sess)
    assignments = await connector_assignment_map(db, user)
    await db.commit()
    return {
        "agents": [_with_thread(serialize_agent(a), latest_agent.get(str(a.id))) for a in agents],
        "groups": [
            _with_thread(serialize_group(g, grouped.get(g.id) or []), latest_group.get(str(g.id))) for g in groups
        ],
        "default_agent_id": str(chief.id),
        "connector_assignments": assignments,
    }


async def get_agent(db: AsyncSession, user: User, agent_id: uuid.UUID) -> WorkspaceAgent:
    await ensure_default_agent(db, user)
    row = await db.get(WorkspaceAgent, agent_id)
    if row is None or row.user_id != user.id:
        raise AgentError("Unknown agent")
    return row


async def get_group(db: AsyncSession, user: User, group_id: uuid.UUID) -> tuple[AgentGroup, list[WorkspaceAgent]]:
    row = await db.get(AgentGroup, group_id)
    if row is None or row.user_id != user.id:
        raise AgentError("Unknown group")
    links = (
        (await db.execute(select(AgentGroupMember).where(AgentGroupMember.group_id == row.id)))
        .scalars()
        .all()
    )
    agents = []
    for link in links:
        agent = await db.get(WorkspaceAgent, link.agent_id)
        if agent is not None and agent.user_id == user.id:
            agents.append(agent)
    return row, agents


async def create_agent(db: AsyncSession, user: User, body: dict) -> WorkspaceAgent:
    await ensure_default_agent(db, user)
    count = len(
        (await db.execute(select(WorkspaceAgent.id).where(WorkspaceAgent.user_id == user.id))).scalars().all()
    )
    if count >= 20:
        raise AgentError("You can have at most 20 agents.")
    name = _clean_name(body.get("name") or "")
    clash = (
        await db.execute(select(WorkspaceAgent).where(WorkspaceAgent.user_id == user.id, WorkspaceAgent.name == name))
    ).scalar_one_or_none()
    if clash is not None:
        raise AgentError("An agent with that name already exists.")
    row = WorkspaceAgent(
        user_id=user.id,
        name=name,
        title=_clean_name(body.get("title") or "specialist") if (body.get("title") or "").strip() else "",
        instructions=_clean_instructions(body.get("instructions") or ""),
        color=str(body.get("color") or pick_color(count)),
        kind="custom",
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise AgentError("An agent with that name already exists.") from exc
    await db.refresh(row)
    return row


async def update_agent(db: AsyncSession, user: User, agent_id: uuid.UUID, body: dict) -> WorkspaceAgent:
    row = await get_agent(db, user, agent_id)
    if "name" in body and body["name"] is not None:
        name = _clean_name(body["name"])
        clash = (
            await db.execute(
                select(WorkspaceAgent).where(
                    WorkspaceAgent.user_id == user.id,
                    WorkspaceAgent.name == name,
                    WorkspaceAgent.id != row.id,
                )
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise AgentError("An agent with that name already exists.")
        row.name = name
    if "title" in body and body["title"] is not None:
        title = (body.get("title") or "").strip()
        row.title = _clean_name(title) if title else ""
    if "instructions" in body and body["instructions"] is not None:
        row.instructions = _clean_instructions(body["instructions"])
    if body.get("color"):
        row.color = str(body["color"])[:16]
    if "pinned" in body and body["pinned"] is not None:
        row.pinned = bool(body["pinned"])
    if "archived" in body and body["archived"] is not None:
        if row.kind == "default" and body["archived"]:
            raise AgentError("The default agent cannot be archived.")
        row.archived = bool(body["archived"])
    if "unread" in body and body["unread"] is not None:
        row.unread = bool(body["unread"])
    await db.commit()
    await db.refresh(row)
    return row


async def delete_agent(db: AsyncSession, user: User, agent_id: uuid.UUID) -> None:
    row = await get_agent(db, user, agent_id)
    if row.kind == "default":
        raise AgentError("The default agent cannot be deleted.")
    await db.delete(row)
    await db.commit()


async def create_group(db: AsyncSession, user: User, body: dict) -> tuple[AgentGroup, list[WorkspaceAgent]]:
    await ensure_default_agent(db, user)
    name = _clean_name(body.get("name") or "")
    ids = validate_group_members(body.get("member_ids") or [])
    agents = []
    for uid in ids:
        agent = await db.get(WorkspaceAgent, uid)
        if agent is None or agent.user_id != user.id:
            raise AgentError("Every member must be one of your agents.")
        agents.append(agent)
    row = AgentGroup(
        user_id=user.id,
        name=name,
        outcome=_clean_outcome(body.get("outcome") or ""),
        owner_id=_owner_from_body(body, agents),
    )
    db.add(row)
    await db.flush()
    for agent in agents:
        db.add(AgentGroupMember(group_id=row.id, agent_id=agent.id))
    await db.commit()
    await db.refresh(row)
    return row, agents


async def update_group(db: AsyncSession, user: User, group_id: uuid.UUID, body: dict) -> tuple[AgentGroup, list[WorkspaceAgent]]:
    row, _ = await get_group(db, user, group_id)
    if "name" in body and body["name"] is not None:
        row.name = _clean_name(body["name"])
    if "outcome" in body and body["outcome"] is not None:
        row.outcome = _clean_outcome(body["outcome"])
    members = None
    if "member_ids" in body and body["member_ids"] is not None:
        ids = validate_group_members(body["member_ids"])
        members = []
        for uid in ids:
            agent = await db.get(WorkspaceAgent, uid)
            if agent is None or agent.user_id != user.id:
                raise AgentError("Every member must be one of your agents.")
            members.append(agent)
        existing = (
            (await db.execute(select(AgentGroupMember).where(AgentGroupMember.group_id == row.id))).scalars().all()
        )
        for link in existing:
            await db.delete(link)
        await db.flush()
        for agent in members:
            db.add(AgentGroupMember(group_id=row.id, agent_id=agent.id))
    if "owner_id" in body:
        current = members if members is not None else (await get_group(db, user, row.id))[1]
        row.owner_id = _owner_from_body(body, current)
    await db.commit()
    return await get_group(db, user, row.id)


async def delete_group(db: AsyncSession, user: User, group_id: uuid.UUID) -> None:
    row, _ = await get_group(db, user, group_id)
    await db.delete(row)
    await db.commit()


async def duplicate_agent(db: AsyncSession, user: User, agent_id: uuid.UUID) -> WorkspaceAgent:
    src = await get_agent(db, user, agent_id)
    base = f"{src.name} copy"
    name = base
    n = 2
    while (
        await db.execute(select(WorkspaceAgent).where(WorkspaceAgent.user_id == user.id, WorkspaceAgent.name == name))
    ).scalar_one_or_none() is not None:
        name = f"{base} {n}"
        n += 1
        if n > 30:
            raise AgentError("Could not pick a unique name for the copy.")
    row = await create_agent(
        db,
        user,
        {"name": name, "title": src.title or "", "instructions": src.instructions or "", "color": src.color},
    )
    links = (await db.execute(select(AgentConnector).where(AgentConnector.agent_id == src.id))).scalars().all()
    for link in links:
        db.add(AgentConnector(agent_id=row.id, connector_id=link.connector_id))
    if links:
        await db.commit()
        await db.refresh(row)
    return row


async def clear_conversation(db: AsyncSession, user: User, agent_id: uuid.UUID) -> None:
    agent = await get_agent(db, user, agent_id)
    sessions = (
        (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.user_id == user.id,
                    ChatSession.agent_id == agent.id,
                    ChatSession.group_id.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    ids = [s.id for s in sessions]
    if ids:
        await db.execute(sql_delete(ChatMessage).where(ChatMessage.session_id.in_(ids)))
        await db.execute(sql_delete(ChatSession).where(ChatSession.id.in_(ids)))
    agent.unread = False
    await db.commit()


async def add_agent_to_group(db: AsyncSession, user: User, agent_id: uuid.UUID, group_id: uuid.UUID) -> None:
    agent = await get_agent(db, user, agent_id)
    group, members = await get_group(db, user, group_id)
    if any(m.id == agent.id for m in members):
        return
    if len(members) >= GROUP_MEMBER_MAX:
        raise AgentError(f"A group needs {GROUP_MEMBER_MIN} to {GROUP_MEMBER_MAX} agents.")
    db.add(AgentGroupMember(group_id=group.id, agent_id=agent.id))
    await db.commit()


def agent_may_use_connector(agent_id, connector_id, assignments: dict) -> bool:
    allowed = assignments.get(str(connector_id)) or []
    if not allowed:
        return True
    return str(agent_id) in allowed


async def connector_assignment_map(db: AsyncSession, user: User) -> dict[str, list[str]]:
    agents = (await db.execute(select(WorkspaceAgent.id).where(WorkspaceAgent.user_id == user.id))).scalars().all()
    agent_ids = {str(i) for i in agents}
    conns = (await db.execute(select(Connector.id).where(Connector.user_id == user.id))).scalars().all()
    if not agent_ids or not conns:
        return {}
    links = (
        (
            await db.execute(
                select(AgentConnector).where(
                    AgentConnector.agent_id.in_(list(agents)),
                    AgentConnector.connector_id.in_(list(conns)),
                )
            )
        )
        .scalars()
        .all()
    )
    out: dict[str, list[str]] = {}
    for link in links:
        out.setdefault(str(link.connector_id), []).append(str(link.agent_id))
    return {key: [aid for aid in ids if aid in agent_ids] for key, ids in out.items()}


async def set_connector_agents(db: AsyncSession, user: User, connector_id: uuid.UUID, agent_ids: list) -> dict:
    conn = await db.get(Connector, connector_id)
    if conn is None or conn.user_id != user.id:
        raise AgentError("Unknown connector")
    agents = (await db.execute(select(WorkspaceAgent).where(WorkspaceAgent.user_id == user.id))).scalars().all()
    wanted: list[uuid.UUID] = []
    seen: set[str] = set()
    for raw in agent_ids or []:
        try:
            uid = uuid.UUID(str(raw))
        except (TypeError, ValueError) as exc:
            raise AgentError("Each agent id must be valid.") from exc
        if str(uid) in seen:
            continue
        seen.add(str(uid))
        if not any(a.id == uid for a in agents):
            raise AgentError("Every assignee must be one of your agents.")
        wanted.append(uid)
    await db.execute(sql_delete(AgentConnector).where(AgentConnector.connector_id == conn.id))
    if wanted and len(wanted) < len(agents):
        for uid in wanted:
            db.add(AgentConnector(agent_id=uid, connector_id=conn.id))
    await db.commit()
    return {"connector_id": str(conn.id), "agent_ids": [str(i) for i in wanted] if wanted and len(wanted) < len(agents) else []}


def filter_connectors(connectors: list, agent_id=None, teammate_ids: list | None = None, assignments: dict | None = None):
    """Keep connectors this agent (or any teammate) is allowed to use."""
    mapping = assignments or {}
    ids = [str(i) for i in (teammate_ids or []) if i]
    if agent_id:
        ids = list(dict.fromkeys([str(agent_id), *ids]))
    if not ids:
        return list(connectors)
    kept = []
    for conn in connectors:
        cid = str(getattr(conn, "id", "") or "")
        allowed = mapping.get(cid) or []
        if not allowed or any(aid in allowed for aid in ids):
            kept.append(conn)
    return kept
