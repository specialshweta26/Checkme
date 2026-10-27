"""Per-user deploy CLI credentials (Connectors page). Values go in, never come back out."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import deploy
from app.db import get_db
from app.deps import current_user
from app.models import DeployCredential, User
from app.security import decrypt_json, encrypt

router = APIRouter(prefix="/connectors/deploy", tags=["deploy"])
MAX_VALUE = 20000


class SaveIn(BaseModel):
    values: dict[str, str]


async def _row(db: AsyncSession, user: User, key: str) -> DeployCredential | None:
    return (
        await db.execute(select(DeployCredential).where(DeployCredential.user_id == user.id, DeployCredential.platform == key))
    ).scalar_one_or_none()


@router.get("")
async def list_platforms(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(DeployCredential).where(DeployCredential.user_id == user.id))).scalars().all()
    saved = {r.platform: sorted(k for k, v in decrypt_json(r.credentials_enc).items() if v) for r in rows}
    return {"platforms": deploy.platforms_public(saved)}


@router.put("/{platform}")
async def save(platform: str, body: SaveIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    try:
        p = deploy.platform(platform)
    except deploy.DeployError as exc:
        raise HTTPException(404, str(exc)) from exc
    allowed = set(deploy.env_names(p))
    bad = [k for k in body.values if k not in allowed]
    if bad:
        raise HTTPException(400, f"Unknown fields for {p.name}: {', '.join(bad)}")
    row = await _row(db, user, p.key)
    current = decrypt_json(row.credentials_enc) if row else {}
    for k, v in body.values.items():
        v = (v or "").strip()
        if len(v) > MAX_VALUE:
            raise HTTPException(400, f"{k} is too long")
        if v:
            current[k] = v  # blank keeps the saved value
    missing = [e for e, _ in p.env if not current.get(e)]
    if missing:
        raise HTTPException(400, f"{p.name} needs {', '.join(missing)}")
    if row is None:
        row = DeployCredential(user_id=user.id, platform=p.key, credentials_enc=encrypt(current))
        db.add(row)
    else:
        row.credentials_enc = encrypt(current)
    await db.commit()
    await _resume(db, user, p.key)
    return {"platform": p.key, "saved": sorted(current), "connected": True}


@router.delete("/{platform}")
async def forget(platform: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    try:
        p = deploy.platform(platform)
    except deploy.DeployError as exc:
        raise HTTPException(404, str(exc)) from exc
    row = await _row(db, user, p.key)
    if row is not None:
        await db.delete(row)
        await db.commit()
    return {"platform": p.key, "connected": False}


async def _resume(db: AsyncSession, user: User, key: str) -> None:
    """Jobs that stopped with ask_user(waiting_for='deploy_<platform>') continue."""
    from app.jobs import service as jobs
    from app.models import AgentJob

    rows = (await db.execute(select(AgentJob).where(AgentJob.user_id == user.id, AgentJob.status == "waiting_input"))).scalars().all()
    note = f"The user connected {deploy.PLATFORMS[key].name} for deploys. Continue: propose the deploy again with the deploy tool (it asks for approval)."
    n = 0
    for job in rows:
        for what in (f"deploy_{key}", f"login_{key}"):
            for ev in jobs.resume_waiting_for(job, what, note):
                db.add(ev)
                n += 1
    if n:
        await db.commit()
        from app.jobs.worker import wake

        wake()
