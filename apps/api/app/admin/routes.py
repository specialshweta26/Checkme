"""The admin console's API (docs/ADMIN.md): people, today's use, invites, suspension and the audit log.

Only the emails in ADMIN_EMAILS get in; anyone else gets a 404, so the console's existence isn't advertised.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import audit
from app.auth import access
from app.config import get_settings
from app.db import get_db
from app.deps import current_user
from app.models import AuditEvent, AuthSession, ChatMessage, SignupInvite, UsageDaily, User

router = APIRouter(prefix="/admin", tags=["admin"])


async def admin_user(user: User = Depends(current_user)) -> User:
    if not access.is_admin(user):
        raise HTTPException(404, "Not found")
    return user


def _today() -> date:
    return datetime.now(timezone.utc).date()


async def _usage_today(db: AsyncSession, ids: list) -> dict:
    """Tokens and cost today per user: stored counters plus those not written yet."""
    from app.agent import meter

    out: dict = {}
    for uid in ids:
        spend = await meter.day_spend(db, uid)
        out[uid] = {"tokens": spend["tokens"], "cost_usd": spend["cost_usd"]}
    return out


@router.get("/overview")
async def overview(_: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    from app.agent import meter

    now = datetime.now(timezone.utc)
    total = (await db.execute(select(func.count(User.id)))).scalar_one()
    new_7d = (await db.execute(select(func.count(User.id)).where(User.created_at >= now - timedelta(days=7)))).scalar_one()
    suspended = (await db.execute(select(func.count(User.id)).where(User.suspended_at.is_not(None)))).scalar_one()
    active = (await db.execute(select(func.count(func.distinct(UsageDaily.user_id))).where(UsageDaily.day == _today()))).scalar_one()
    tokens = (await db.execute(select(func.coalesce(func.sum(UsageDaily.input_tokens + UsageDaily.output_tokens), 0))
                               .where(UsageDaily.day == _today()))).scalar_one()  # fmt: skip
    usd, cap_tokens = meter.user_cap()
    s = get_settings()
    return {
        "users": total, "new_7d": new_7d, "suspended": suspended, "active_today": active, "tokens_today": int(tokens),
        "signup_mode": access.mode(), "signup_allow_env": [x for x in (s.signup_allow or "").replace(";", ",").split(",") if x.strip()],
        "cap": {"tokens": cap_tokens or None, "usd": usd or None},
    }  # fmt: skip


@router.get("/users")
async def users(q: str = "", _: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    from app.agent import meter

    stmt = select(User).order_by(User.created_at.desc()).limit(200)
    if q.strip():
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(func.lower(User.email).like(like), func.lower(User.display_name).like(like)))
    rows = list((await db.execute(stmt)).scalars().all())
    ids = [u.id for u in rows]
    seen = dict((await db.execute(select(AuthSession.user_id, func.max(AuthSession.created_at))
                                  .where(AuthSession.user_id.in_(ids)).group_by(AuthSession.user_id))).all()) if ids else {}  # fmt: skip
    use = await _usage_today(db, ids)
    usd, cap_tokens = meter.user_cap()
    return [
        {
            "id": str(u.id), "email": u.email, "name": u.display_name, "provider": u.primary_provider,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_sign_in": seen[u.id].isoformat() if seen.get(u.id) else None,
            "suspended": u.suspended_at is not None, "admin": access.is_admin(u),
            "today": use.get(u.id, {}), "exempt": meter.cap_exempt(u.email),
            "over_cap": bool(not meter.cap_exempt(u.email) and ((cap_tokens and use.get(u.id, {}).get("tokens", 0) >= cap_tokens)
                             or (usd and (use.get(u.id, {}).get("cost_usd") or 0) >= usd))),
        }
        for u in rows
    ]  # fmt: skip


class SuspendIn(BaseModel):
    suspended: bool


@router.post("/users/{user_id}/suspend")
async def suspend(user_id: uuid.UUID, body: SuspendIn, request: Request, admin: User = Depends(admin_user),
                  db: AsyncSession = Depends(get_db)):  # fmt: skip
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "No such user.")
    if target.id == admin.id:
        raise HTTPException(400, "You can't suspend yourself.")
    target.suspended_at = datetime.now(timezone.utc) if body.suspended else None
    if body.suspended:
        ids = list((await db.execute(select(AuthSession.id).where(AuthSession.user_id == target.id, AuthSession.revoked.is_(False)))).scalars())
        if ids:
            await db.execute(update(AuthSession).where(AuthSession.id.in_(ids)).values(revoked=True))
            try:
                from app.cache import get_redis

                r = get_redis()
                for sid in ids:
                    await r.set(f"revoked:{sid}", b"1", ex=60 * 60 * 24 * 31)
            except Exception:  # noqa: BLE001 - the suspension still blocks every request
                pass
        from app.agent import meter

        meter._cap_cache.pop(target.id, None)
    audit.record(db, "user_suspended" if body.suspended else "user_restored", target.id, admin.id, target=target.email, request=request)
    await db.commit()
    return {"ok": True, "suspended": body.suspended}


class InviteIn(BaseModel):
    value: str
    note: str = ""
    send: bool = False


def _clean_invite(value: str) -> str:
    v = (value or "").strip().lower().lstrip("@")
    if "@" in v:
        local, _, dom = v.rpartition("@")
        ok = bool(local) and "." in dom
    else:
        ok = "." in v and " " not in v and len(v) <= 253
    if not ok or len(v) > 320:
        raise HTTPException(400, "Enter an email address or a domain like acme.com.")
    return v


@router.get("/latency")
async def latency(days: int = 1, _: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    """Reply times (app/admin/latency.py): how many chat replies took longer than REPLY_TARGET_S, and what took the
    slowest ones their time. Message text is never returned."""
    from app.admin import latency as lat

    since = datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 30)))
    rows = (
        await db.execute(
            select(ChatMessage.created_at, User.email, ChatMessage.meta["timing"])
            .join(User, User.id == ChatMessage.user_id)
            .where(ChatMessage.role == "assistant", ChatMessage.created_at >= since, ChatMessage.meta.has_key("timing"))
            .order_by(ChatMessage.created_at.desc())
            .limit(5000)
        )
    ).all()
    return lat.summarize([tuple(r) for r in rows], float(get_settings().reply_target_s or 45))


class ScenarioRunIn(BaseModel):
    ids: list[str] = []
    custom: list[str] = []


@router.get("/scenarios")
async def scenarios(admin: User = Depends(admin_user)):
    """The test questions (app/admin/scenarios.json) and this admin's latest run."""
    from app.admin import scenarios as sc

    return {"scenarios": sc.defaults(), "run": sc.state(admin.id)}


@router.post("/scenarios/run")
async def run_scenarios(body: ScenarioRunIn, request: Request, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    """Run the chosen test questions (all when none are chosen) plus the admin's own, one after another, as the admin.
    Reads only: nothing is confirmed on their behalf."""
    from app.admin import scenarios as sc

    run = sc.start(admin.id, body.ids, body.custom)
    audit.record(db, "admin_scenarios_run", user_id=admin.id, actor_id=admin.id, target=run["id"],
                       detail={"questions": len(run["results"])}, request=request)  # fmt: skip
    await db.commit()
    return run


@router.post("/scenarios/stop")
async def stop_scenarios(admin: User = Depends(admin_user)):
    from app.admin import scenarios as sc

    return sc.stop(admin.id) or {"status": "idle"}


@router.get("/invites")
async def invites(_: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(SignupInvite).order_by(SignupInvite.created_at.desc()))).scalars().all()
    return [{"id": str(r.id), "value": r.value, "note": r.note, "created_at": r.created_at.isoformat()} for r in rows]


@router.post("/invites")
async def add_invite(body: InviteIn, request: Request, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    value = _clean_invite(body.value)
    row = (await db.execute(select(SignupInvite).where(SignupInvite.value == value))).scalar_one_or_none()
    if row is None:
        row = SignupInvite(value=value, note=(body.note or "").strip()[:200], invited_by=admin.id)
        db.add(row)
    audit.record(db, "invite_added", None, admin.id, target=value, request=request)
    await db.commit()
    sent = False
    if body.send and "@" in value:
        from app.auth import mailer

        link = f"{get_settings().public_web_url}/signup"
        text, page = mailer.render("You're invited to Lumio",
                                   [f"{admin.display_name or admin.email} invited you to Lumio, an assistant for your inbox, "
                                    "calendar and the apps you work in.", f"Sign up with this address ({value})."],
                                   "Create your account", link)  # fmt: skip
        sent = await mailer.send(value, "You're invited to Lumio", text, page) if mailer.configured() else False
    return {"ok": True, "id": str(row.id), "value": value, "sent": sent}


@router.delete("/invites/{invite_id}")
async def remove_invite(invite_id: uuid.UUID, request: Request, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    row = await db.get(SignupInvite, invite_id)
    if row is None:
        raise HTTPException(404, "No such invite.")
    audit.record(db, "invite_removed", None, admin.id, target=row.value, request=request)
    await db.delete(row)
    await db.commit()
    return {"ok": True}


async def _audit_rows(db: AsyncSession, user_id=None, days: int = 30, action: str = "") -> list:
    since = datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 365)))
    stmt = select(AuditEvent).where(AuditEvent.at >= since).order_by(AuditEvent.at.desc()).limit(5000)
    if user_id is not None:
        stmt = stmt.where(or_(AuditEvent.user_id == user_id, AuditEvent.actor_id == user_id))
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    return list((await db.execute(stmt)).scalars().all())


async def _emails(db: AsyncSession, rows: list) -> dict:
    ids = {x for r in rows for x in (r.user_id, r.actor_id) if x}
    if not ids:
        return {}
    return dict((await db.execute(select(User.id, User.email).where(User.id.in_(ids)))).all())


def _json(rows: list, emails: dict) -> list:
    return [{"at": r.at.isoformat(), "action": r.action, "user": emails.get(r.user_id, ""), "actor": emails.get(r.actor_id, ""),
             "target": r.target, "detail": r.detail or {}, "ip": r.ip} for r in rows]  # fmt: skip


@router.get("/audit")
async def audit_log(days: int = 30, action: str = "", user: str = "", fmt: str = "json", _: User = Depends(admin_user),
                    db: AsyncSession = Depends(get_db)):  # fmt: skip
    uid = None
    if user:
        try:
            uid = uuid.UUID(user)
        except ValueError:
            raise HTTPException(400, "user must be a user id")
    rows = await _audit_rows(db, uid, days, action)
    emails = await _emails(db, rows)
    if fmt == "csv":
        return Response(audit.to_csv(rows, emails), media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="lumio-audit.csv"'})  # fmt: skip
    return _json(rows[:500], emails)


me_router = APIRouter(prefix="/me", tags=["me"])


@me_router.get("/audit")
async def my_audit(days: int = 90, fmt: str = "json", user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Your own audit log: sign-ins, password changes, admin actions on your account. CSV with fmt=csv."""
    rows = await _audit_rows(db, user.id, days)
    emails = await _emails(db, rows)
    if fmt == "csv":
        return Response(audit.to_csv(rows, emails), media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="lumio-my-activity.csv"'})  # fmt: skip
    return _json(rows[:500], emails)
