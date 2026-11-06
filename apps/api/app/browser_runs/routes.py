"""Saved browser runs (docs/BROWSING.md "Healing saved runs"): list them, and accept, reject or undo a fix the
replay found when a site changed."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.agent.fast_browser import STORE
from app.deps import current_user
from app.models import User

router = APIRouter(prefix="/browser/saved-runs", tags=["browser"])


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
