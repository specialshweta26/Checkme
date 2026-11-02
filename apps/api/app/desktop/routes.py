"""HTTP API for the Lumio desktop sidecar (pair, heartbeat, job poll)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import current_user
from app.desktop import agent as desktop
from app.models import User

router = APIRouter(prefix="/desktop", tags=["desktop"])


class PairIn(BaseModel):
    hostname: str = ""
    os: str = ""


class PreferIn(BaseModel):
    target: str = Field(default="local")


class JobResultIn(BaseModel):
    id: str
    result: dict = Field(default_factory=dict)


async def _agent_user(
    db: AsyncSession = Depends(get_db),
    x_lumio_agent: str | None = Header(default=None, alias="X-Lumio-Agent"),
) -> tuple:
    try:
        return await desktop.authorize_token(db, x_lumio_agent or "")
    except LookupError as exc:
        raise HTTPException(401, str(exc)) from exc


@router.post("/pair")
async def pair(body: PairIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await desktop.pair(db, user, hostname=body.hostname, os_name=body.os)


@router.get("/status")
async def status(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await desktop.status(db, user)


@router.post("/prefer")
async def prefer(body: PreferIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    try:
        return await desktop.set_prefer(db, user, body.target)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/heartbeat")
async def heartbeat(pair=Depends(_agent_user)):
    user, row = pair
    await desktop.heartbeat(user.id, desktop.stored_hash(row))
    return {"ok": True, "prefer": desktop.stored_prefer(row)}


@router.get("/jobs")
async def jobs(wait: int = 20, pair=Depends(_agent_user)):
    user, row = pair
    await desktop.heartbeat(user.id, desktop.stored_hash(row))
    job = await desktop.take_job(user.id, wait_s=wait)
    return {"ok": True, "job": job}


class ScreenIn(BaseModel):
    png_b64: str = ""
    url: str = ""
    title: str = ""


@router.post("/screen")
async def screen(body: ScreenIn, pair=Depends(_agent_user)):
    import base64

    from app.connectors.daytona_box import remember_screen

    user, row = pair
    await desktop.heartbeat(user.id, desktop.stored_hash(row))
    if not body.png_b64:
        raise HTTPException(400, "png required")
    try:
        png = base64.b64decode(body.png_b64)
    except Exception as vis_exc:  # noqa: BLE001
        raise HTTPException(400, "invalid png") from vis_exc
    remember_screen(png, str(user.id))
    return {"ok": True, "url": body.url, "title": body.title}


class TeachEventsIn(BaseModel):
    events: list[dict] = Field(default_factory=list)
    url: str = ""
    title: str = ""


@router.post("/teach-events")
async def teach_events(body: TeachEventsIn, pair=Depends(_agent_user)):
    from app.skills import recorder

    user, row = pair
    await desktop.heartbeat(user.id, desktop.stored_hash(row))
    events = list(body.events or [])
    if body.title:
        events.append({"kind": "window", "window": body.title[:200]})
    n = recorder.add_local_events(str(user.id), events)
    return {"ok": True, "n": n}


@router.post("/jobs/result")
async def job_result(body: JobResultIn, pair=Depends(_agent_user)):
    user, row = pair
    await desktop.heartbeat(user.id, desktop.stored_hash(row))
    if not body.id.strip():
        raise HTTPException(400, "job id required")
    await desktop.put_result(body.id.strip(), body.result if isinstance(body.result, dict) else {})
    return {"ok": True}
