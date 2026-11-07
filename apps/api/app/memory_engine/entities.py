"""People and organisations in one user's world, recognised by email, name and email domain.

Every row carries user_id and every lookup filters on it: two users who both email priya@acme.com each get their
own "Priya". Aliases are unique per user (uq_memory_alias_user_kind_value), so when two writers try to create the same
person at the same time, the second one finds the first one's entity instead of making a duplicate.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
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


# --- the user's own other addresses ----------------------------------------------------------------------------------
# A person entity with status "self" is the user under another address (a personal Gmail, an alias). Its email aliases
# count as the user's own, so the learner never counts mail to or from them as a contact. An alias of kind "not_me"
# records that the user said an address is someone else, so the look-alike guess below never hides it again.

_NAME_SPLIT = re.compile(r"[^a-z]+")


def first_name_of(user) -> str:
    """The user's first name, from their display name or else their email (gaurav.r@… → gaurav); "" if too short."""
    for source in (getattr(user, "display_name", "") or "", (getattr(user, "email", "") or "").split("@")[0]):
        tokens = [t for t in _NAME_SPLIT.split(source.lower()) if t]
        if tokens and len(tokens[0]) >= 3:
            return tokens[0]
    return ""


def looks_like_user(name: str, email: str, user) -> bool:
    """A personal-mail address whose name and mailbox both carry the user's first name (Gaurav <gaurav.grv@gmail.com>
    for user Gaurav): most likely the user's own other address. A guess, so it is shown and can be undone."""
    first = first_name_of(user)
    email = clean_email(email)
    if not first or not email or email.split("@")[1] not in FREE_MAIL:
        return False
    tokens = [t for t in _NAME_SPLIT.split((name or "").lower()) if t]
    return bool(tokens) and tokens[0] == first and first in email.split("@")[0]


def named_like_user(name: str, user) -> bool:
    """The display name starts with the user's first name ("Gaurav R" for user Gaurav). Without a name, no."""
    first = first_name_of(user)
    tokens = [t for t in _NAME_SPLIT.split((name or "").lower()) if t]
    return bool(first and tokens and tokens[0] == first)


async def self_addresses(db: AsyncSession, user_id) -> set[str]:
    rows = (await db.execute(
        select(MemoryEntityAlias.value)
        .join(MemoryEntity, MemoryEntity.id == MemoryEntityAlias.entity_id)
        .where(MemoryEntityAlias.user_id == user_id, MemoryEntityAlias.kind == "email",
               MemoryEntity.user_id == user_id, MemoryEntity.status == "self")
    )).scalars().all()
    return set(rows)


async def _entity_for(db: AsyncSession, user_id, who: str) -> MemoryEntity | None:
    """This user's person entity (any status but merged/deleted) for an email address or a name."""
    email, name = clean_email(who), clean_name(who)
    kind, value = ("email", email) if email else ("name", name.lower())
    if not value:
        return None
    return (await db.execute(
        select(MemoryEntity).join(MemoryEntityAlias, MemoryEntityAlias.entity_id == MemoryEntity.id)
        .where(MemoryEntityAlias.user_id == user_id, MemoryEntityAlias.kind == kind, MemoryEntityAlias.value == value,
               MemoryEntity.user_id == user_id, MemoryEntity.status.in_(("active", "self")), MemoryEntity.kind == "person")
    )).scalars().first()


async def set_self(db: AsyncSession, user_id, who: str, is_me: bool = True, *, guessed: bool = False,
                   by_user: bool = False) -> MemoryEntity | None:
    """Mark the person behind an address or name as the user (is_me) or as someone else. A guess never overrides the
    user saying "that's not me". An unknown address the user says is theirs is recorded so it is skipped from now on."""
    if db is None or not user_id or not (who or "").strip():
        return None
    entity = await _entity_for(db, user_id, who)
    if entity is None:
        email = clean_email(who)
        if not (is_me and email):
            return None
        entity = await _create(db, user_id, "person", email.split("@")[0], [("email", email)])
    if is_me:
        if guessed:
            said_not_me = (await db.execute(
                select(MemoryEntityAlias.id).where(MemoryEntityAlias.user_id == user_id, MemoryEntityAlias.entity_id == entity.id,
                                                   MemoryEntityAlias.kind == "not_me")
            )).first()
            if said_not_me:
                return None
        entity.status = "self"
    else:
        entity.status = "active"
    if not guessed:
        # Keep only the user's latest word on it: "is_me" or "not_me".
        said, unsaid = ("is_me", "not_me") if is_me else ("not_me", "is_me")
        await db.execute(delete(MemoryEntityAlias).where(
            MemoryEntityAlias.user_id == user_id, MemoryEntityAlias.entity_id == entity.id, MemoryEntityAlias.kind == unsaid))
        await _claim_alias(db, user_id, entity.id, said, f"entity:{entity.id}")
    if by_user:
        from app.memory_engine.consolidate import record

        await record(db, user_id, "self", f"entity:{entity.id}", "", entity.name if is_me else "",
                     "you said this is you" if is_me else "you said this is someone else")
    await db.flush()
    return entity


async def guessed_self(db: AsyncSession, user_id) -> list[tuple[str, str]]:
    """[(name, email)] of addresses hidden as the user's own by the look-alike guess (not by the user saying so)."""
    confirmed = select(MemoryEntityAlias.entity_id).where(MemoryEntityAlias.user_id == user_id, MemoryEntityAlias.kind == "is_me")
    rows = (await db.execute(
        select(MemoryEntity.name, MemoryEntityAlias.value)
        .join(MemoryEntityAlias, MemoryEntityAlias.entity_id == MemoryEntity.id)
        .where(MemoryEntity.user_id == user_id, MemoryEntity.status == "self", MemoryEntityAlias.kind == "email",
               MemoryEntityAlias.user_id == user_id, MemoryEntity.id.not_in(confirmed))
        .order_by(MemoryEntity.salience.desc(), MemoryEntity.id)
    )).all()
    return [(r[0], r[1]) for r in rows]


async def unmark_wrong_self(db: AsyncSession, user) -> int:
    """Undo "this is the user" where the user never said so and the name isn't theirs. The first Sent-folder rule took
    any From in Sent Items as the user, which is wrong for mail sent on someone's behalf or by a delegate."""
    from app.models import MemoryChange

    said = select(MemoryChange.target).where(MemoryChange.user_id == user.id, MemoryChange.op == "self")
    rows = (await db.execute(
        select(MemoryEntity).join(MemoryEntityAlias, MemoryEntityAlias.entity_id == MemoryEntity.id)
        .where(MemoryEntity.user_id == user.id, MemoryEntity.status == "self", MemoryEntityAlias.user_id == user.id,
               MemoryEntityAlias.kind == "is_me")
    )).scalars().all()
    targets = set((await db.execute(said)).scalars().all())
    fixed = 0
    for e in rows:
        emails = [a for a in (await db.execute(select(MemoryEntityAlias.value).where(
            MemoryEntityAlias.user_id == user.id, MemoryEntityAlias.entity_id == e.id, MemoryEntityAlias.kind == "email"))).scalars()]
        if f"entity:{e.id}" in targets or named_like_user(e.name, user) or any(looks_like_user(e.name, m, user) for m in emails):
            continue
        e.status = "active"
        await db.execute(delete(MemoryEntityAlias).where(
            MemoryEntityAlias.user_id == user.id, MemoryEntityAlias.entity_id == e.id, MemoryEntityAlias.kind == "is_me"))
        from app.memory_engine.consolidate import record

        await record(db, user.id, "self", f"entity:{e.id}", "", "", f"{e.name} was wrongly taken for your own address")
        fixed += 1
    await db.flush()
    return fixed


async def mark_lookalikes(db: AsyncSession, user) -> int:
    """Hide person entities learned before that look like the user's own other address. Returns how many."""
    rows = (await db.execute(
        select(MemoryEntity, MemoryEntityAlias.value)
        .join(MemoryEntityAlias, MemoryEntityAlias.entity_id == MemoryEntity.id)
        .where(MemoryEntity.user_id == user.id, MemoryEntity.status == "active", MemoryEntity.kind == "person",
               MemoryEntityAlias.user_id == user.id, MemoryEntityAlias.kind == "email")
    )).all()
    marked = 0
    for entity, email in rows:
        if entity.status == "active" and looks_like_user(entity.name, email, user):
            if await set_self(db, user.id, email, True, guessed=True) is not None:
                marked += 1
    return marked


_TOP_PEOPLE = re.compile(
    r"\bwho\b[^?.!]{0,60}\b(?:most|most often|often|frequently|the most)\b|"
    r"\b(?:my )?(?:top|main|key|closest|most important) (?:\d+ )?(?:contacts|people|colleagues|clients)\b",
    re.I,
)
_STOP = frozenset({"the", "and", "for", "with", "from", "about", "what", "who", "when", "where", "which", "this", "that",
                   "have", "has", "had", "you", "your", "our", "their", "them", "they", "email", "emails", "mail",
                   "meeting", "meetings", "calendar", "today", "tomorrow", "week", "please", "tell", "show", "send"})


def _ago(when: datetime | None) -> str:
    if when is None:
        return "no contact recorded"
    days = (datetime.now(timezone.utc) - when).days
    return "today" if days <= 0 else ("yesterday" if days == 1 else f"{days} days ago")


async def people_context(db: AsyncSession, user_id, query: str, limit: int = 5) -> list[str]:
    """One line per person the question is about (named in it), or the user's most important people when asked for
    them: how much they interact, when last, and who they meet with. Only this user's entities."""
    from app.models import MemoryRelation

    if db is None or not (query or "").strip():
        return []
    rows: list = []
    top = bool(_TOP_PEOPLE.search(query))
    if top:
        rows = (await db.execute(
            select(MemoryEntity).where(MemoryEntity.user_id == user_id, MemoryEntity.status == "active", MemoryEntity.kind == "person")
            .order_by(MemoryEntity.salience.desc()).limit(limit)
        )).scalars().all()
    else:
        # Plain letters only, so nothing in the question needs escaping inside the Postgres regex below.
        words = [w.lower() for w in re.findall(r"[A-Za-z]{3,}", query) if w.lower() not in _STOP]
        if not words:
            return []
        rows = (await db.execute(
            select(MemoryEntity).where(
                MemoryEntity.user_id == user_id, MemoryEntity.status == "active",
                func.lower(MemoryEntity.name).op("~")(r"\m(" + "|".join(words[:12]) + r")\M"),
            ).order_by(MemoryEntity.salience.desc()).limit(limit)
        )).scalars().all()
    if not rows:
        return []
    org_names = await names_for(db, user_id, {r.org_id for r in rows if r.org_id})
    lines = []
    for e in rows:
        partners = (await db.execute(
            select(MemoryEntity.name)
            .join(MemoryRelation, (MemoryRelation.b_id == MemoryEntity.id) | (MemoryRelation.a_id == MemoryEntity.id))
            .where(MemoryRelation.user_id == user_id, (MemoryRelation.a_id == e.id) | (MemoryRelation.b_id == e.id),
                   MemoryEntity.id != e.id, MemoryEntity.status == "active")
            .order_by(MemoryRelation.count.desc()).limit(3)
        )).scalars().all()
        org = f" ({org_names[e.org_id]})" if e.org_id in org_names else ""
        if e.kind != "person":
            lines.append(f"{e.name}: {e.kind}")
            continue
        bits = [f"you emailed them {e.mail_out or 0}×", f"they emailed you {e.mail_in or 0}×", f"{e.meetings or 0} meetings"]
        line = f"{e.name}{org}: " + ", ".join(bits) + f"; last contact {_ago(e.last_interaction_at)}"
        if partners:
            line += "; often meets with " + ", ".join(partners)
        if (e.summary or "").strip():
            line += f". Card: {e.summary.strip()}"
        lines.append(line)
    if top:
        hidden = await guessed_self(db, user_id)
        if hidden:
            shown = ", ".join(f"{n} <{e}>" if n and n != e.split("@")[0] else e for n, e in hidden[:3])
            lines.append(f"Not counted as contacts, because they look like the user's own other addresses: {shown}. "
                         "Mention this; if the user says one is someone else, call set_my_address with is_me=false.")
    return lines
