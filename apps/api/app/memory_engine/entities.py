"""People and organisations in one user's world, recognised by email, name and email domain.

Every row carries user_id and every lookup filters on it: two users who both email priya@acme.com each get their
own "Priya". Aliases are unique per user (uq_memory_alias_user_kind_value), so when two writers try to create the same
person at the same time, the second one finds the first one's entity instead of making a duplicate.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MemoryEntity, MemoryEntityAlias

log = logging.getLogger(__name__)

KINDS = ("person", "org", "project", "place", "topic")
# Personal mail providers say nothing about where someone works.
FREE_MAIL = frozenset(
    {
        "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "yahoo.com", "yahoo.co.in",
        "icloud.com", "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com", "zoho.com", "yandex.com",
        "gmx.com", "mail.com", "rediffmail.com", "hey.com", "fastmail.com",
    }
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def clean_email(value: str) -> str:
    m = _EMAIL.search(value or "")
    return m.group(0).lower() if m else ""


def clean_name(value: str) -> str:
    return " ".join((value or "").split())[:200]


def work_domain(email: str) -> str:
    """The organisation domain of a work address (acme.com for priya@mail.acme.com), or "" for personal mail."""
    m = _EMAIL.search(email or "")
    if not m:
        return ""
    domain = m.group(1).lower()
    if domain in FREE_MAIL:
        return ""
    parts = domain.split(".")
    # Keep the registrable part: acme.com, acme.co.in.
    keep = 3 if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in {"co", "com", "org", "net", "ac", "gov"} else 2
    return ".".join(parts[-keep:])


def org_name_for(domain: str) -> str:
    return domain.split(".")[0].replace("-", " ").title() if domain else ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _by_alias(db: AsyncSession, user_id, kind: str, value: str) -> MemoryEntity | None:
    if not value:
        return None
    row = (
        await db.execute(
            select(MemoryEntity)
            .join(MemoryEntityAlias, MemoryEntityAlias.entity_id == MemoryEntity.id)
            .where(
                MemoryEntityAlias.user_id == user_id,
                MemoryEntityAlias.kind == kind,
                MemoryEntityAlias.value == value.lower(),
                MemoryEntity.user_id == user_id,
                MemoryEntity.status == "active",
            )
        )
    ).scalars().first()
    return row


async def _claim_alias(db: AsyncSession, user_id, entity_id: int, kind: str, value: str) -> int:
    """Attach an alias; if another entity of this user already has it, return that entity's id."""
    stmt = (
        pg_insert(MemoryEntityAlias)
        .values(user_id=user_id, entity_id=entity_id, kind=kind, value=value.lower()[:320])
        .on_conflict_do_nothing(constraint="uq_memory_alias_user_kind_value")
        .returning(MemoryEntityAlias.entity_id)
    )
    got = (await db.execute(stmt)).scalar_one_or_none()
    if got is not None:
        return got
    existing = (
        await db.execute(
            select(MemoryEntityAlias.entity_id).where(
                MemoryEntityAlias.user_id == user_id, MemoryEntityAlias.kind == kind, MemoryEntityAlias.value == value.lower()
            )
        )
    ).scalar_one_or_none()
    return existing if existing is not None else entity_id


async def _create(db: AsyncSession, user_id, kind: str, name: str, aliases: list[tuple[str, str]]) -> MemoryEntity:
    entity = MemoryEntity(user_id=user_id, kind=kind, name=name, last_seen_at=_now())
    db.add(entity)
    await db.flush()
    winner = entity.id
    for alias_kind, value in aliases:
        if not value:
            continue
        owner = await _claim_alias(db, user_id, winner, alias_kind, value)
        if owner != winner:
            # Someone else created this person a moment ago: use theirs and drop ours.
            await db.delete(entity)
            await db.flush()
            found = await db.get(MemoryEntity, owner)
            return found
    return entity


async def resolve(db: AsyncSession, user_id, *, name: str = "", email: str = "", kind: str = "person") -> MemoryEntity | None:
    """Find or create the entity this user means by (name, email). Email wins over name; a person with a work email
    is linked to the organisation of that domain."""
    if db is None or not user_id:
        return None
    kind = kind if kind in KINDS else "person"
    email = clean_email(email)
    name = clean_name(name)
    if not email and not name:
        return None
    entity = await _by_alias(db, user_id, "email", email) if email else None
    if entity is None and name:
        entity = await _by_alias(db, user_id, "name", name)
        if entity is not None and entity.kind != kind:
            entity = None
    if entity is None:
        entity = await _create(db, user_id, kind, name or email.split("@")[0], [("email", email), ("name", name)])
    else:
        if email:
            await _claim_alias(db, user_id, entity.id, "email", email)
        if name:
            await _claim_alias(db, user_id, entity.id, "name", name)
        if name and (not entity.name or "@" in entity.name or entity.name == email.split("@")[0]):
            entity.name = name
        entity.last_seen_at = _now()
    domain = work_domain(email)
    if entity is not None and kind == "person" and domain:
        # The newest work email decides the organisation: someone who moves from acme.com to zetacorp.com moves too.
        org = await _by_alias(db, user_id, "domain", domain)
        if org is None:
            org = await _create(db, user_id, "org", org_name_for(domain), [("domain", domain)])
        if org is not None and entity.org_id != org.id:
            entity.org_id = org.id
    await db.flush()
    return entity


async def names_for(db: AsyncSession, user_id, ids: set[int]) -> dict[int, str]:
    """{entity id: display name} for this user's entities (other users' ids return nothing)."""
    ids = {i for i in ids if i}
    if db is None or not ids:
        return {}
    rows = (
        await db.execute(select(MemoryEntity.id, MemoryEntity.name).where(MemoryEntity.user_id == user_id, MemoryEntity.id.in_(ids)))
    ).all()
    return {row[0]: row[1] for row in rows}
