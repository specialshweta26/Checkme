"""GitHub (through Composio) status and connect routes for the Connectors page."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.connectors import github as gh
from app.db import get_db
from app.deps import current_user
from app.models import User

log = logging.getLogger(__name__)
router = APIRouter(prefix="/connectors/github", tags=["github"])
WAITING_FOR = ("github", "composio_github", "github_app")


async def github_status(user: User) -> dict:
    """Is GitHub connected through Composio, as whom, and how will sandbox git authenticate. No secrets."""
    out: dict = {"configured": gh.is_configured(), "connected": False, "login": "", "git_transport": "", "error": ""}
    if not out["configured"]:
        out["error"] = "COMPOSIO_API_KEY is not set on this server."
        return out
    try:
        acct = await gh.account_for(user.id, fresh=True)
    except gh.NotConnected:
        return out
    except Exception as exc:  # noqa: BLE001
        out["error"] = gh._scrub(str(exc))[:300]
        return out
    out["connected"] = True
    out["managed_auth"] = acct.managed
    try:
        out["login"] = (await gh.viewer(user.id)).get("login") or ""
    except Exception as exc:  # noqa: BLE001
        out["error"] = gh._scrub(str(exc))[:300]
    out["git_transport"] = "git" if await gh.git_token(user.id) else "api"
    return out


async def resume_blocked_jobs(db: AsyncSession, user: User, login: str = "") -> int:
    """Jobs that stopped because GitHub was not connected pick up where they left off."""
    from app.jobs import service as jobs
    from app.models import AgentJob

    try:
        rows = (
            await db.execute(select(AgentJob).where(AgentJob.user_id == user.id, AgentJob.status == "waiting_input"))
        ).scalars().all()
        note = (
            f"The user connected GitHub through Composio{' as @' + login if login else ''}. Continue where you stopped: "
            "push the saved branch with push_branch (it asks for approval), open the PR, and finish the task."
        )
        n = 0
        for job in rows:
            for key in WAITING_FOR:
                for ev in jobs.resume_waiting_for(job, key, note):
                    db.add(ev)
                    n += 1
        if n:
            await db.commit()
            from app.jobs.worker import wake

            wake()
        return n
    except Exception as exc:  # noqa: BLE001
        log.warning("resuming jobs after GitHub connected failed: %s", exc)
        return 0


@router.get("/status")
async def status(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    out = await github_status(user)
    if out["connected"]:
        out["resumed_jobs"] = await resume_blocked_jobs(db, user, out.get("login") or "")
    return out


@router.post("/connect")
async def connect(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Start Composio's GitHub OAuth link; the browser comes back to /connectors?composio=connected&toolkit=github."""
    from app.connectors.composio_box import start_link
    from app.connectors.platform import ensure_platform_connectors

    if not gh.is_configured():
        raise HTTPException(400, "COMPOSIO_API_KEY is not configured")
    await ensure_platform_connectors(db, user)
    callback = f"{get_settings().public_web_url.rstrip('/')}/connectors?composio=connected&toolkit=github"
    try:
        data = await start_link(str(user.id), gh.TOOLKIT, callback)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, gh._scrub(str(exc))[:300])
    if not data.get("redirect_url"):
        raise HTTPException(400, "Composio did not return a GitHub sign-in URL.")
    return {"redirect_url": data["redirect_url"]}
