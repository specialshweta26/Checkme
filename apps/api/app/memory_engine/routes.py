"""What I know: the user's view of their own memory — people, facts, and what changed — with edit, undo and forget.

Every route reads and writes only the signed-in user's rows (user_id filters on every query); an id that belongs to
another user answers 404, the same as one that doesn't exist.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import current_user
from app.memory_engine import entities as ent
from app.memory_engine.consolidate import effective_confidence, record
from app.models import MemoryChange, MemoryEntity, MemoryEntityAlias, MemoryPassage, MemoryRelation, User

router = APIRouter(prefix="/memory", tags=["memory"])

USER_SCOPES = ("user", "user_shared")
UNDOABLE = {"add", "supersede", "fade", "card"}
OP_LABELS = {
    "add": "Learned", "supersede": "Updated", "fade": "Faded", "merge": "Merged", "card": "Card rewritten",
    "self": "Marked as you", "forget": "Forgotten",
}


def _iso(when: datetime | None) -> str:
    return when.isoformat() if when else ""


def _fact(row: MemoryPassage) -> dict:
    return {
        "id": row.id, "text": row.text, "status": row.status, "trust": row.trust, "predicate": row.predicate,
        "confidence": effective_confidence(row), "reinforced": row.reinforced,
        "valid_from": _iso(row.valid_from or row.created_at), "valid_to": _iso(row.valid_to),
        "subject_id": row.subject_entity_id,
    }


async def _emails(db: AsyncSession, user_id, ids: list[int]) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    if ids:
        for eid, value in (await db.execute(select(MemoryEntityAlias.entity_id, MemoryEntityAlias.value).where(
                MemoryEntityAlias.user_id == user_id, MemoryEntityAlias.kind == "email",
                MemoryEntityAlias.entity_id.in_(ids)))).all():
            out.setdefault(eid, []).append(value)
    return out


def _person(e: MemoryEntity, orgs: dict[int, str], emails: dict[int, list[str]], facts: dict[int, int]) -> dict:
    return {
        "id": e.id, "name": e.name, "org": orgs.get(e.org_id, ""), "emails": sorted(emails.get(e.id, [])),
        "card": e.summary or "", "mail_in": e.mail_in or 0, "mail_out": e.mail_out or 0, "meetings": e.meetings or 0,
        "last_contact": _iso(e.last_interaction_at), "salience": e.salience, "facts": facts.get(e.id, 0),
        "is_me": e.status == "self",
    }


async def _owned(db: AsyncSession, model, user: User, row_id: int):
    row = await db.get(model, row_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(404, "Not found")
    return row


@router.get("/people")
async def people(q: str = "", user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """The user's people, most important first, and the addresses memory treats as the user's own."""
    cond = [MemoryEntity.user_id == user.id, MemoryEntity.kind == "person", MemoryEntity.status.in_(("active", "self"))]
    if q.strip():
        cond.append(func.lower(MemoryEntity.name).contains(q.strip().lower()[:80]))
    rows = (await db.execute(select(MemoryEntity).where(*cond).order_by(MemoryEntity.salience.desc(), MemoryEntity.id).limit(100))).scalars().all()
    ids = [e.id for e in rows]
    orgs = await ent.names_for(db, user.id, {e.org_id for e in rows if e.org_id})
    emails = await _emails(db, user.id, ids)
    counts = dict((await db.execute(select(MemoryPassage.subject_entity_id, func.count()).where(
        MemoryPassage.user_id == user.id, MemoryPassage.status == "active", MemoryPassage.subject_entity_id.in_(ids or [-1]))
        .group_by(MemoryPassage.subject_entity_id))).all())
    listed = [_person(e, orgs, emails, counts) for e in rows]
    return {"people": [p for p in listed if not p["is_me"]], "me": [p for p in listed if p["is_me"]]}


@router.get("/people/{entity_id}")
async def person(entity_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    e = await _owned(db, MemoryEntity, user, entity_id)
    if e.kind != "person" or e.status not in ("active", "self"):
        raise HTTPException(404, "Not found")
    facts = (await db.execute(select(MemoryPassage).where(
        MemoryPassage.user_id == user.id, MemoryPassage.subject_entity_id == e.id,
        MemoryPassage.status.in_(("active", "superseded", "faded"))).order_by(MemoryPassage.id.desc()).limit(60))).scalars().all()
    partners = (await db.execute(
        select(MemoryEntity.name, MemoryRelation.count)
        .join(MemoryRelation, (MemoryRelation.b_id == MemoryEntity.id) | (MemoryRelation.a_id == MemoryEntity.id))
        .where(MemoryRelation.user_id == user.id, (MemoryRelation.a_id == e.id) | (MemoryRelation.b_id == e.id),
               MemoryEntity.id != e.id, MemoryEntity.status == "active")
        .order_by(MemoryRelation.count.desc()).limit(8))).all()
    orgs = await ent.names_for(db, user.id, {e.org_id} if e.org_id else set())
    return {
        **_person(e, orgs, await _emails(db, user.id, [e.id]), {e.id: sum(1 for f in facts if f.status == "active")}),
        "current": [_fact(f) for f in facts if f.status == "active"],
        "history": [_fact(f) for f in facts if f.status != "active"],
        "meets_with": [{"name": n, "count": c} for n, c in partners],
    }


class SelfIn(BaseModel):
    is_me: bool = True


@router.post("/people/{entity_id}/me")
async def mark_me(entity_id: int, body: SelfIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    e = await _owned(db, MemoryEntity, user, entity_id)
    emails = (await _emails(db, user.id, [e.id])).get(e.id) or []
    row = await ent.set_self(db, user.id, emails[0] if emails else e.name, body.is_me)
    if row is None:
        raise HTTPException(404, "Not found")
    await record(db, user.id, "self", f"entity:{e.id}", "", e.name if body.is_me else "", "you said this is you" if body.is_me else "you said this is someone else")
    await db.commit()
    return {"ok": True, "is_me": body.is_me}


@router.delete("/people/{entity_id}")
async def forget_person(entity_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Forget a person everywhere: their record, addresses, every fact about them, cards and logged copies."""
    from app.agent.memory import _forget_everywhere
    from app.agents.service import ensure_default_agent

    e = await _owned(db, MemoryEntity, user, entity_id)
    emails = (await _emails(db, user.id, [e.id])).get(e.id) or []
    owner = await ensure_default_agent(db, user)
    removed = await _forget_everywhere(db, user, owner, e.name)
    for email in emails:
        removed += await _forget_everywhere(db, user, owner, email)
    e.status = "deleted"
    await record(db, user.id, "forget", f"entity:{e.id}", "", "", "you asked to forget a person")
    await db.commit()
    return {"ok": True, "removed": len(set(removed))}


@router.get("/facts")
async def facts(q: str = "", user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """What memory holds about the user's world (shared by all their agents), newest first."""
    cond = [MemoryPassage.user_id == user.id, MemoryPassage.status == "active", MemoryPassage.scope.in_(USER_SCOPES)]
    if q.strip():
        cond.append(func.lower(MemoryPassage.text).contains(q.strip().lower()[:80]))
    rows = (await db.execute(select(MemoryPassage).where(*cond).order_by(MemoryPassage.id.desc()).limit(200))).scalars().all()
    names = await ent.names_for(db, user.id, {r.subject_entity_id for r in rows if r.subject_entity_id})
    return {"facts": [{**_fact(r), "about": names.get(r.subject_entity_id, "")} for r in rows]}


class FactIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


@router.patch("/facts/{fact_id}")
async def edit_fact(fact_id: int, body: FactIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Correct a fact: the corrected text replaces it (the old one is kept as history) and counts as said by the user."""
    from app.agent.memory import remember

    old = await _owned(db, MemoryPassage, user, fact_id)
    if old.status != "active" or old.scope not in USER_SCOPES:
        raise HTTPException(404, "Not found")
    row = await remember(db, user.id, old.kind or "archival", body.text.strip(), supersedes_id=old.id,
                         subject_entity_id=old.subject_entity_id, predicate=old.predicate, trust="user_said")
    if row is None:
        raise HTTPException(400, "Could not save")
    return {"ok": True, "fact": _fact(row)}


@router.delete("/facts/{fact_id}")
async def delete_fact(fact_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    row = await _owned(db, MemoryPassage, user, fact_id)
    if row.status == "deleted":
        return {"ok": True}
    row.status, row.valid_to = "deleted", datetime.now(timezone.utc)
    # Logged copies of a deleted fact's text go with it; the log keeps only that something was deleted.
    await db.execute(MemoryChange.__table__.delete().where(MemoryChange.user_id == user.id, MemoryChange.target == f"fact:{row.id}"))
    await record(db, user.id, "forget", f"fact:{row.id}", "", "", "you deleted a fact")
    await db.commit()
    return {"ok": True}


@router.get("/suggestions")
async def suggestions(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """People worth a quick note: worked with a lot, quiet for three weeks or more."""
    from app.memory_engine.routines import reconnect

    return {"reconnect": await reconnect(db, user.id)}


@router.get("/changes")
async def changes(days: int = 7, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """What memory changed in the last `days` days (the weekly digest), newest first, with counts by kind."""
    since = datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 90)))
    rows = (await db.execute(select(MemoryChange).where(MemoryChange.user_id == user.id, MemoryChange.created_at >= since)
                             .order_by(MemoryChange.id.desc()).limit(300))).scalars().all()
    counts: dict[str, int] = {}
    for c in rows:
        if not c.undone_at:
            counts[c.op] = counts.get(c.op, 0) + 1
    return {
        "since": since.isoformat(),
        "counts": counts,
        "changes": [{
            "id": c.id, "op": c.op, "label": OP_LABELS.get(c.op, c.op), "target": c.target, "before": c.before,
            "after": c.after, "reason": c.reason, "at": _iso(c.created_at), "undone": bool(c.undone_at),
            "can_undo": c.op in UNDOABLE and not c.undone_at,
        } for c in rows],
    }


@router.post("/changes/{change_id}/undo")
async def undo(change_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    c = await _owned(db, MemoryChange, user, change_id)
    if c.undone_at or c.op not in UNDOABLE:
        raise HTTPException(409, "This change can't be undone.")
    kind, _, raw = c.target.partition(":")
    if not raw.isdigit():
        raise HTTPException(409, "This change can't be undone.")
    now = datetime.now(timezone.utc)
    if kind == "fact":
        row = await _owned(db, MemoryPassage, user, int(raw))
        if c.op == "add":
            row.status, row.valid_to = "deleted", now
        elif c.op in ("supersede", "fade"):
            if c.op == "supersede":
                # Retire what replaced it: the active fact with the newer text.
                newer = (await db.execute(select(MemoryPassage).where(
                    MemoryPassage.user_id == user.id, MemoryPassage.status == "active",
                    func.lower(MemoryPassage.text) == (c.after or "").lower()))).scalars().first()
                if newer is not None:
                    newer.status, newer.valid_to = "superseded", now
            row.status, row.valid_to, row.last_confirmed_at = "active", None, now  # confirmed: it won't fade again soon
    elif kind == "entity" and c.op == "card":
        e = await _owned(db, MemoryEntity, user, int(raw))
        e.summary, e.card_built_at = c.before or "", now
    else:
        raise HTTPException(409, "This change can't be undone.")
    c.undone_at = now
    await db.commit()
    return {"ok": True}
