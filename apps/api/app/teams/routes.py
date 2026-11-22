"""Teams (docs/TEAMS.md): members with roles, emailed invitations, shared skills and the team's activity log.

What a team shares is deliberately small. Each person's mail, calendar, connectors, agents and memory stay their own.
A shared skill is a cleaned copy (steps and instructions, no screenshots or narration), and using it means adding
your own copy, which then runs with your own connectors.

Roles: owner (one; everything, including deleting the team), admin (members, invites, the activity log), member
(shares and uses skills), viewer (uses skills only).
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import audit
from app.config import get_settings
from app.db import get_db
from app.deps import current_user
from app.models import AgentSkill, AuditEvent, Team, TeamInvite, TeamMember, TeamSkill, User

router = APIRouter(prefix="/teams", tags=["teams"])

ROLES = ("owner", "admin", "member", "viewer")
RANK = {r: i for i, r in enumerate(reversed(ROLES))}  # viewer 0 … owner 3
INVITE_TTL = timedelta(days=7)
MAX_MEMBERS = 200


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _membership(db: AsyncSession, team_id: uuid.UUID, user: User, at_least: str = "viewer") -> tuple[Team, TeamMember]:
    team = await db.get(Team, team_id)
    m = await db.get(TeamMember, (team_id, user.id)) if team else None
    if team is None or m is None:
        raise HTTPException(404, "No such team.")  # never says whether a team you're not in exists
    if RANK.get(m.role, 0) < RANK[at_least]:
        raise HTTPException(403, f"You need to be a team {at_least} for that.")
    return team, m


def _log(db, action: str, team: Team, actor: User, target: str = "", request: Request | None = None, **detail) -> None:
    audit.record(db, f"team_{action}", actor.id, actor.id, target=target, detail={"team": str(team.id), **detail}, request=request)


class TeamIn(BaseModel):
    name: str


def _name(value: str) -> str:
    name = " ".join((value or "").split())[:120]
    if not name:
        raise HTTPException(400, "Give the team a name.")
    return name


@router.get("")
async def my_teams(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Team, TeamMember.role).join(TeamMember, TeamMember.team_id == Team.id)
                             .where(TeamMember.user_id == user.id).order_by(Team.created_at))).all()  # fmt: skip
    counts = dict((await db.execute(select(TeamMember.team_id, func.count()).where(TeamMember.team_id.in_([t.id for t, _ in rows]))
                                    .group_by(TeamMember.team_id))).all()) if rows else {}  # fmt: skip
    return [{"id": str(t.id), "name": t.name, "role": role, "members": counts.get(t.id, 1)} for t, role in rows]


@router.post("")
async def create_team(body: TeamIn, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    owned = (await db.execute(select(func.count()).select_from(Team).where(Team.owner_id == user.id))).scalar_one()
    if owned >= 10:
        raise HTTPException(400, "You can own up to 10 teams.")
    team = Team(name=_name(body.name), owner_id=user.id)
    db.add(team)
    await db.flush()
    db.add(TeamMember(team_id=team.id, user_id=user.id, role="owner"))
    _log(db, "created", team, user, team.name, request)
    await db.commit()
    return {"id": str(team.id), "name": team.name, "role": "owner"}


@router.get("/{team_id}")
async def team_detail(team_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    team, me = await _membership(db, team_id, user)
    members = (await db.execute(select(TeamMember, User).join(User, User.id == TeamMember.user_id)
                                .where(TeamMember.team_id == team.id).order_by(TeamMember.joined_at))).all()  # fmt: skip
    skills = (await db.execute(select(TeamSkill).where(TeamSkill.team_id == team.id).order_by(TeamSkill.created_at.desc()))).scalars().all()
    names = {u.id: (u.display_name or u.email) for _, u in members}
    out = {
        "id": str(team.id), "name": team.name, "role": me.role,
        "members": [{"user_id": str(u.id), "name": u.display_name, "email": u.email, "role": m.role,
                     "joined_at": m.joined_at.isoformat()} for m, u in members],
        "skills": [{"id": str(s.id), "name": s.name, "kind": s.kind, "description": s.description,
                    "steps": len(s.steps or []), "shared_by": names.get(s.shared_by, "a former member"),
                    "mine": s.shared_by == user.id, "created_at": s.created_at.isoformat()} for s in skills],
        "invites": [],
    }  # fmt: skip
    if RANK[me.role] >= RANK["admin"]:
        now = datetime.now(timezone.utc)
        inv = (await db.execute(select(TeamInvite).where(TeamInvite.team_id == team.id, TeamInvite.accepted_at.is_(None))
                                .order_by(TeamInvite.created_at.desc()))).scalars().all()  # fmt: skip
        out["invites"] = [{"id": str(i.id), "email": i.email, "role": i.role, "expired": i.created_at + INVITE_TTL < now} for i in inv]
    return out


@router.patch("/{team_id}")
async def rename(team_id: uuid.UUID, body: TeamIn, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    team, _ = await _membership(db, team_id, user, "admin")
    team.name = _name(body.name)
    _log(db, "renamed", team, user, team.name, request)
    await db.commit()
    return {"ok": True}


@router.delete("/{team_id}")
async def delete_team(team_id: uuid.UUID, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    team, _ = await _membership(db, team_id, user, "owner")
    _log(db, "deleted", team, user, team.name, request)
    await db.delete(team)
    await db.commit()
    return {"ok": True}


class InviteIn(BaseModel):
    email: str
    role: str = "member"


@router.post("/{team_id}/invites")
async def invite(team_id: uuid.UUID, body: InviteIn, request: Request, user: User = Depends(current_user),
                 db: AsyncSession = Depends(get_db)):  # fmt: skip
    from app.auth import mailer
    from app.auth.email import normalize_email

    team, me = await _membership(db, team_id, user, "admin")
    role = body.role if body.role in ("admin", "member", "viewer") else "member"
    if role == "admin" and me.role != "owner":
        raise HTTPException(403, "Only the owner can invite admins.")
    email = normalize_email(body.email)
    count = (await db.execute(select(func.count()).select_from(TeamMember).where(TeamMember.team_id == team.id))).scalar_one()
    if count >= MAX_MEMBERS:
        raise HTTPException(400, f"A team can have up to {MAX_MEMBERS} members.")
    already = (await db.execute(select(TeamMember).join(User, User.id == TeamMember.user_id)
                                .where(TeamMember.team_id == team.id, func.lower(User.email) == email))).first()  # fmt: skip
    if already:
        raise HTTPException(409, "They're already in this team.")
    await db.execute(delete(TeamInvite).where(TeamInvite.team_id == team.id, func.lower(TeamInvite.email) == email,
                                              TeamInvite.accepted_at.is_(None)))  # fmt: skip
    token = secrets.token_urlsafe(32)
    db.add(TeamInvite(team_id=team.id, email=email, role=role, token_hash=_hash(token), invited_by=user.id))
    _log(db, "invited", team, user, email, request, role=role)
    await db.commit()
    link = f"{get_settings().public_web_url}/teams/join?token={token}"
    sent = False
    if mailer.configured():
        text, page = mailer.render(
            f"Join {team.name} on Lumio",
            [f"{user.display_name or user.email} invited you to the team {team.name} on Lumio, as {role}.",
             f"Sign in or sign up with {email} to accept. The link works for 7 days."],  # fmt: skip
            "Join the team", link,
        )
        sent = await mailer.send(email, f"Join {team.name} on Lumio", text, page)
    return {"ok": True, "sent": sent, "link": link}


@router.delete("/{team_id}/invites/{invite_id}")
async def cancel_invite(team_id: uuid.UUID, invite_id: uuid.UUID, request: Request, user: User = Depends(current_user),
                        db: AsyncSession = Depends(get_db)):  # fmt: skip
    team, _ = await _membership(db, team_id, user, "admin")
    inv = await db.get(TeamInvite, invite_id)
    if inv is None or inv.team_id != team.id:
        raise HTTPException(404, "No such invite.")
    _log(db, "invite_cancelled", team, user, inv.email, request)
    await db.delete(inv)
    await db.commit()
    return {"ok": True}


class JoinIn(BaseModel):
    token: str


@router.post("/join")
async def join(body: JoinIn, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    inv = (await db.execute(select(TeamInvite).where(TeamInvite.token_hash == _hash(body.token or "")))).scalar_one_or_none()
    if inv is None or inv.accepted_at is not None or inv.created_at + INVITE_TTL < datetime.now(timezone.utc):
        raise HTTPException(400, "This invitation has expired or was already used. Ask for a new one.")
    if inv.email.lower() != (user.email or "").lower():
        raise HTTPException(403, f"This invitation is for {inv.email}. Sign in with that address to accept it.")
    team = await db.get(Team, inv.team_id)
    if team is None:
        raise HTTPException(400, "That team no longer exists.")
    if await db.get(TeamMember, (team.id, user.id)) is None:
        db.add(TeamMember(team_id=team.id, user_id=user.id, role=inv.role))
    inv.accepted_at = datetime.now(timezone.utc)
    _log(db, "joined", team, user, user.email, request, role=inv.role)
    await db.commit()
    return {"ok": True, "team_id": str(team.id), "name": team.name}


class RoleIn(BaseModel):
    role: str


@router.patch("/{team_id}/members/{member_id}")
async def set_role(team_id: uuid.UUID, member_id: uuid.UUID, body: RoleIn, request: Request, user: User = Depends(current_user),
                   db: AsyncSession = Depends(get_db)):  # fmt: skip
    team, me = await _membership(db, team_id, user, "admin")
    m = await db.get(TeamMember, (team.id, member_id))
    if m is None:
        raise HTTPException(404, "Not a member.")
    if body.role not in ("admin", "member", "viewer"):
        raise HTTPException(400, "Role must be admin, member or viewer. To hand over the team, transfer ownership.")
    if m.role == "owner":
        raise HTTPException(400, "The owner's role changes only by transferring ownership.")
    if me.role != "owner" and ("admin" in (m.role, body.role)):
        raise HTTPException(403, "Only the owner can make or change admins.")
    old, m.role = m.role, body.role
    _log(db, "role_changed", team, user, str(member_id), request, old=old, new=body.role)
    await db.commit()
    return {"ok": True}


@router.delete("/{team_id}/members/{member_id}")
async def remove_member(team_id: uuid.UUID, member_id: uuid.UUID, request: Request, user: User = Depends(current_user),
                        db: AsyncSession = Depends(get_db)):  # fmt: skip
    leaving = member_id == user.id
    team, me = await _membership(db, team_id, user, "viewer" if leaving else "admin")
    m = await db.get(TeamMember, (team.id, member_id))
    if m is None:
        raise HTTPException(404, "Not a member.")
    if m.role == "owner":
        raise HTTPException(400, "The owner can't leave. Transfer ownership first, or delete the team.")
    if not leaving and m.role == "admin" and me.role != "owner":
        raise HTTPException(403, "Only the owner can remove an admin.")
    _log(db, "left" if leaving else "removed", team, user, str(member_id), request)
    await db.delete(m)
    await db.commit()
    return {"ok": True}


class OwnerIn(BaseModel):
    user_id: uuid.UUID


@router.post("/{team_id}/owner")
async def transfer(team_id: uuid.UUID, body: OwnerIn, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    team, me = await _membership(db, team_id, user, "owner")
    new = await db.get(TeamMember, (team.id, body.user_id))
    if new is None or new.user_id == user.id:
        raise HTTPException(400, "Pick another member of this team.")
    new.role, me.role, team.owner_id = "owner", "admin", new.user_id
    _log(db, "owner_changed", team, user, str(new.user_id), request)
    await db.commit()
    return {"ok": True}


class ShareIn(BaseModel):
    skill_id: uuid.UUID


@router.post("/{team_id}/skills")
async def share_skill(team_id: uuid.UUID, body: ShareIn, request: Request, user: User = Depends(current_user),
                      db: AsyncSession = Depends(get_db)):  # fmt: skip
    from app.agent.integrity import blocked
    from app.skills.service import mask_secrets

    team, _ = await _membership(db, team_id, user, "member")
    src = await db.get(AgentSkill, body.skill_id)
    if src is None or src.user_id != user.id:
        raise HTTPException(404, "No such skill.")
    if (src.status or "ready") != "ready":
        raise HTTPException(400, "Finish this skill before sharing it.")
    if blocked(src.instruction or "") or blocked(src.description or ""):
        raise HTTPException(400, "This skill changes or hides figures, so it can't be shared.")
    copy = TeamSkill(team_id=team.id, shared_by=user.id, source_skill_id=src.id, name=src.name, kind=src.kind,
                     description=src.description or "", instruction=src.instruction or "",
                     steps=mask_secrets(list(src.steps or [])), flow=dict(src.flow or {}), files=list(src.files or []))  # fmt: skip
    db.add(copy)
    _log(db, "skill_shared", team, user, src.name, request)
    await db.commit()
    return {"ok": True, "id": str(copy.id)}


@router.delete("/{team_id}/skills/{shared_id}")
async def unshare_skill(team_id: uuid.UUID, shared_id: uuid.UUID, request: Request, user: User = Depends(current_user),
                        db: AsyncSession = Depends(get_db)):  # fmt: skip
    team, me = await _membership(db, team_id, user)
    row = await db.get(TeamSkill, shared_id)
    if row is None or row.team_id != team.id:
        raise HTTPException(404, "No such shared skill.")
    if row.shared_by != user.id and RANK[me.role] < RANK["admin"]:
        raise HTTPException(403, "Only whoever shared it, or an admin, can remove it.")
    _log(db, "skill_unshared", team, user, row.name, request)
    await db.delete(row)
    await db.commit()
    return {"ok": True}


class AddIn(BaseModel):
    agent_id: uuid.UUID | None = None


@router.post("/{team_id}/skills/{shared_id}/add")
async def add_skill(team_id: uuid.UUID, shared_id: uuid.UUID, body: AddIn, request: Request, user: User = Depends(current_user),
                    db: AsyncSession = Depends(get_db)):  # fmt: skip
    from app.models import WorkspaceAgent

    team, _ = await _membership(db, team_id, user)
    row = await db.get(TeamSkill, shared_id)
    if row is None or row.team_id != team.id:
        raise HTTPException(404, "No such shared skill.")
    if body.agent_id is not None:
        agent = await db.get(WorkspaceAgent, body.agent_id)
        if agent is None or agent.user_id != user.id:
            raise HTTPException(404, "No such agent.")
    mine = AgentSkill(user_id=user.id, agent_id=body.agent_id, name=row.name, kind=row.kind, steps=list(row.steps or []),
                      instruction=row.instruction, description=row.description, flow=dict(row.flow or {}),
                      files=list(row.files or []), status="ready",
                      source={"team": str(team.id), "team_name": team.name, "shared_skill": str(row.id)})  # fmt: skip
    db.add(mine)
    _log(db, "skill_added", team, user, row.name, request)
    await db.commit()
    return {"ok": True, "skill_id": str(mine.id)}


@router.get("/{team_id}/activity")
async def activity(team_id: uuid.UUID, days: int = 90, fmt: str = "json", user: User = Depends(current_user),
                   db: AsyncSession = Depends(get_db)):  # fmt: skip
    """The team's activity log: joins, roles, invites and shared skills. Members' own sign-ins and chats aren't in it."""
    team, _ = await _membership(db, team_id, user, "admin")
    since = datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 365)))
    rows = list((await db.execute(select(AuditEvent).where(AuditEvent.at >= since, AuditEvent.action.like("team_%"),
                                                           AuditEvent.detail["team"].astext == str(team.id))
                                  .order_by(AuditEvent.at.desc()).limit(5000))).scalars())  # fmt: skip
    ids = {x for r in rows for x in (r.user_id, r.actor_id) if x}
    emails = dict((await db.execute(select(User.id, User.email).where(User.id.in_(ids)))).all()) if ids else {}
    if fmt == "csv":
        return Response(audit.to_csv(rows, emails), media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="lumio-team-activity.csv"'})  # fmt: skip
    return [{"at": r.at.isoformat(), "action": r.action.removeprefix("team_"), "by": emails.get(r.actor_id, ""),
             "target": r.target, "detail": {k: v for k, v in (r.detail or {}).items() if k != "team"}} for r in rows[:300]]  # fmt: skip
