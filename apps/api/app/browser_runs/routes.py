"""Saved browser runs (docs/BROWSING.md "Healing saved runs"): list them, and accept, reject or undo a fix the
replay found when a site changed."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

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
