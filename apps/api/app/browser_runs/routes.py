"""Saved browser runs (docs/BROWSING.md "Healing saved runs"): list them, and accept, reject or undo a fix the
replay found when a site changed."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db

from app.agent.fast_browser import STORE
from app.browser_runs import logins
from app.deps import current_user
from app.models import User

router = APIRouter(prefix="/browser/saved-runs", tags=["browser"])


class LoginIn(BaseModel):
    host: str
    username: str
    password: str = ""
    label: str = ""


login_router = APIRouter(prefix="/browser/logins", tags=["browser"])
runs_router = APIRouter(prefix="/browser/runs", tags=["browser"])


@runs_router.get("")
async def list_runs(trace_id: str = "", user: User = Depends(current_user)) -> list[dict]:
    """Recent browser runs, newest first (``trace_id``: the runs of one saved run)."""
    from app.browser_runs import runlog

    return await runlog.list_runs(user.id, trace_id)


@runs_router.get("/{run_id}")
async def get_run(run_id: str, user: User = Depends(current_user)) -> dict:
    """One run step by step: action, reason, result, page and a screenshot link."""
    from app.browser_runs import runlog

    run = await runlog.get_run(user.id, run_id)
    if run is None:
        raise HTTPException(404, "no such run")
    return run


@login_router.get("")
async def list_logins(user: User = Depends(current_user)) -> list[dict]:
    """Saved sign-ins: site, label and a username hint. Passwords never leave the server."""
    return await logins.list_logins(user.id)


@login_router.post("")
async def save_login(body: LoginIn, user: User = Depends(current_user)) -> dict:
    try:
        return await logins.save_login(user.id, body.host, body.username, body.password, body.label)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@login_router.delete("/{login_id}")
async def delete_login(login_id: str, user: User = Depends(current_user)) -> dict:
    if not await logins.delete_login(user.id, login_id):
        raise HTTPException(404, "no such saved sign-in")
    return {"ok": True}


class RunIn(BaseModel):
    values: dict = {}
    rows: list[dict] = []


hook_router = APIRouter(prefix="/run-hooks", tags=["browser"])


@hook_router.post("/{trace_id}")
async def run_by_key(trace_id: str, body: RunIn, authorization: str = Header(default=""), db: AsyncSession = Depends(get_db)) -> dict:
    """Start a saved run from another system: Authorization: Bearer <run key>, body {values, rows}."""
    from app.browser_runs import launch

    key = authorization.removeprefix("Bearer ").strip()
    try:
        user, trace = await launch.trace_for_key(db, trace_id, key)
        rows = launch.clean_rows(trace["params"], body.values, body.rows)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from None
    except launch.RunError as exc:
        raise HTTPException(400, str(exc)) from None
    return await launch.queue_run(db, user, trace, rows, "the API")


class FixIn(BaseModel):
    accept: bool


class EditIn(BaseModel):
    steps: list[dict]
    name: str = ""


@router.get("")
async def list_runs(user: User = Depends(current_user)) -> list[dict]:
    return await STORE.listing(user.id)


@router.get("/{trace_id}")
async def get_run(trace_id: str, user: User = Depends(current_user)) -> dict:
    rows = await STORE.listing(user.id, trace_id)
    if not rows:
        raise HTTPException(404, "no such saved run")
    return rows[0]


@router.post("/{trace_id}/fix")
async def resolve_fix(trace_id: str, body: FixIn, user: User = Depends(current_user)) -> dict:
    out = await STORE.resolve(user.id, trace_id, body.accept)
    if not out.get("ok"):
        raise HTTPException(404 if "no such" in str(out.get("error")) else 400, out.get("error"))
    return out


@router.put("/{trace_id}/steps")
async def edit_steps(trace_id: str, body: EditIn, user: User = Depends(current_user)) -> dict:
    """New step list: each {"keep": i} (saved step i) or {"goal": "sentence"} (an AI step)."""
    out = await STORE.edit(user.id, trace_id, body.steps, body.name)
    if not out.get("ok"):
        raise HTTPException(404 if "no such" in str(out.get("error")) else 400, out.get("error"))
    return out["run"]


@router.post("/{trace_id}/run")
async def run_now(trace_id: str, body: RunIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """Run a saved run in the background, once or once per row."""
    from app.browser_runs import launch

    rows = await STORE.candidates(user.id, trace_id=trace_id)
    if not rows:
        raise HTTPException(404, "no such saved run")
    try:
        clean = launch.clean_rows(rows[0].get("params") or [], body.values, body.rows)
    except launch.RunError as exc:
        raise HTTPException(400, str(exc)) from None
    return await launch.queue_run(db, user, rows[0], clean, "the Saved runs page")


@router.post("/{trace_id}/key")
async def make_key(trace_id: str, user: User = Depends(current_user)) -> dict:
    """A new run key for starting this run from other systems (shown once; replaces any old key)."""
    from app.browser_runs import launch

    out = await launch.set_key(user.id, trace_id, rotate=True)
    if not out.get("ok"):
        raise HTTPException(404, out.get("error"))
    return out


@router.delete("/{trace_id}/key")
async def remove_key(trace_id: str, user: User = Depends(current_user)) -> dict:
    from app.browser_runs import launch

    out = await launch.set_key(user.id, trace_id, rotate=False)
    if not out.get("ok"):
        raise HTTPException(404, out.get("error"))
    return out


@router.post("/{trace_id}/undo")
async def undo_fix(trace_id: str, user: User = Depends(current_user)) -> dict:
    out = await STORE.undo(user.id, trace_id)
    if not out.get("ok"):
        raise HTTPException(404 if "no such" in str(out.get("error")) else 400, out.get("error"))
    return out


@router.delete("/{trace_id}")
async def delete_run(trace_id: str, user: User = Depends(current_user)) -> dict:
    if not await STORE.delete(user.id, trace_id):
        raise HTTPException(404, "no such saved run")
    return {"ok": True}
