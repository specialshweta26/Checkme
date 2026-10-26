"""Install, callback, status and webhook routes for the Checkme GitHub App."""

from __future__ import annotations

import json
import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import get_redis
from app.config import get_settings
from app.connectors import github_app as gh
from app.db import get_db
from app.deps import current_user, optional_user
from app.models import Connector, GitHubInstallation, User

log = logging.getLogger(__name__)
router = APIRouter(prefix="/connectors/github", tags=["github"])
_STATE_TTL = 900


def _web() -> str:
    return get_settings().public_web_url.rstrip("/")


async def _stash_state(user_id: str) -> str:
    state = secrets.token_urlsafe(24)
    await get_redis().set(f"gh_install:{state}", json.dumps({"user_id": user_id}).encode(), ex=_STATE_TTL)
    return state


async def _pop_state(state: str | None) -> dict | None:
    if not state:
        return None
    r = get_redis()
    raw = await r.get(f"gh_install:{state}")
    if not raw:
        return None
    await r.delete(f"gh_install:{state}")
    return json.loads(raw)


def serialize_installation(row: GitHubInstallation) -> dict:
    return {
        "installation_id": row.installation_id,
        "account": row.account_login,
        "account_type": row.account_type,
        "repository_selection": row.repository_selection,
        "suspended": row.suspended,
        "manage_url": (
            f"https://github.com/organizations/{row.account_login}/settings/installations/{row.installation_id}"
            if row.account_type == "Organization"
            else f"https://github.com/settings/installations/{row.installation_id}"
        ),
    }


async def github_status(db: AsyncSession, user: User) -> dict:
    rows = (
        await db.execute(select(GitHubInstallation).where(GitHubInstallation.user_id == user.id))
    ).scalars().all()
    return {
        "configured": gh.is_configured(),
        "can_verify": not any(n in gh.missing_env() for n in gh.VERIFY_ENV),
        "missing_env": gh.missing_env(),
        "installed": any(not r.suspended for r in rows),
        "installations": [serialize_installation(r) for r in rows],
        "install_url": gh.checkme_install_link() if gh.is_configured() else "",
        "app_url": f"https://github.com/apps/{get_settings().github_app_slug}" if gh.is_configured() else "",
    }


@router.get("/status")
async def status(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await github_status(db, user)


@router.get("/install")
async def install(user: User = Depends(current_user)):
    if not gh.is_configured():
        return RedirectResponse(f"{_web()}/connectors?error=github_app_not_configured", status_code=302)
    state = await _stash_state(str(user.id))
    return RedirectResponse(gh.app_install_url(state), status_code=302)


async def save_installation(db: AsyncSession, user: User, data: dict) -> GitHubInstallation:
    fields = gh.installation_fields(data)
    row = (
        await db.execute(
            select(GitHubInstallation).where(
                GitHubInstallation.user_id == user.id,
                GitHubInstallation.installation_id == fields["installation_id"],
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = GitHubInstallation(user_id=user.id, **fields)
        db.add(row)
    else:
        for k, v in fields.items():
            setattr(row, k, v)
    await db.flush()
    return row


async def ensure_connector(db: AsyncSession, user: User) -> None:
    from app.connectors.platform import ensure_platform_connectors

    await db.commit()
    await ensure_platform_connectors(db, user)
    await db.commit()


@router.get("/callback")
async def callback(
    request: Request,
    code: str | None = None,
    installation_id: int | None = None,
    setup_action: str | None = None,
    state: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    web = _web()
    if not gh.is_configured():
        return RedirectResponse(f"{web}/connectors?error=github_app_not_configured")
    saved = await _pop_state(state)
    user = None
    if saved:
        import uuid

        user = await db.get(User, uuid.UUID(saved["user_id"]))
    if user is None:
        user = await optional_user(request, db)
    if user is None:
        return RedirectResponse(f"{web}/?error=sign_in_first")
    if setup_action == "request":
        # An org member asked an owner to approve. Nothing to store yet.
        return RedirectResponse(f"{web}/connectors?github=requested")
    if not installation_id:
        return RedirectResponse(f"{web}/connectors?error=github_missing_installation")
    # Never trust installation_id from the query string alone: prove this GitHub user can see it.
    if not code:
        return RedirectResponse(f"{web}/connectors?error=github_needs_user_authorization")
    try:
        user_token = await gh.exchange_code(code)
        visible = await gh.user_installation_ids(user_token)
        if int(installation_id) not in visible:
            return RedirectResponse(f"{web}/connectors?error=github_installation_not_yours")
        data = await gh.app_get(f"/app/installations/{int(installation_id)}")
    except Exception as exc:  # noqa: BLE001
        log.warning("github install callback failed: %s", gh._scrub(str(exc)))
        return RedirectResponse(f"{web}/connectors?error=github_install_failed")
    await save_installation(db, user, data)
    await ensure_connector(db, user)
    await resume_blocked_jobs(db, user, (data.get("account") or {}).get("login") or "")
    return RedirectResponse(f"{web}/connectors?github=connected")


async def resume_blocked_jobs(db: AsyncSession, user: User, account: str) -> int:
    """Jobs that stopped because the GitHub App was missing pick up where they left off."""
    from sqlalchemy import select

    from app.jobs import service as jobs
    from app.models import AgentJob

    try:
        rows = (
            await db.execute(select(AgentJob).where(AgentJob.user_id == user.id, AgentJob.status == "waiting_input"))
        ).scalars().all()
        note = (
            f"The user installed the Checkme GitHub App{' on ' + account if account else ''}. Continue where you stopped: "
            "push the saved branch with push_branch (it asks for approval), open the PR, and finish the task."
        )
        n = 0
        for job in rows:
            for ev in jobs.resume_waiting_for(job, "github_app", note):
                db.add(ev)
                n += 1
        if n:
            await db.commit()
            from app.jobs.worker import wake

            wake()
        return n
    except Exception as exc:  # noqa: BLE001
        log.warning("resuming jobs after the GitHub install failed: %s", exc)
        return 0


@router.delete("/installations/{installation_id}")
async def forget_installation(installation_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Forget the link in Checkme. Uninstalling the App itself happens on GitHub (manage_url)."""
    await db.execute(
        delete(GitHubInstallation).where(
            GitHubInstallation.user_id == user.id, GitHubInstallation.installation_id == installation_id
        )
    )
    left = (
        await db.execute(select(GitHubInstallation.id).where(GitHubInstallation.user_id == user.id).limit(1))
    ).first()
    if left is None:
        await db.execute(delete(Connector).where(Connector.user_id == user.id, Connector.slug == gh.SLUG))
    await db.commit()
    return {"ok": True}


@router.post("/webhook")
async def webhook(request: Request, db: AsyncSession = Depends(get_db)):
    body = await request.body()
    if not gh.verify_webhook(body, request.headers.get("X-Hub-Signature-256", "")):
        raise HTTPException(401, "bad signature")
    event = request.headers.get("X-GitHub-Event", "")
    payload = json.loads(body or b"{}")
    inst = payload.get("installation") or {}
    inst_id = inst.get("id")
    action = payload.get("action")
    if event == "installation" and inst_id:
        rows = (
            await db.execute(select(GitHubInstallation).where(GitHubInstallation.installation_id == int(inst_id)))
        ).scalars().all()
        if action == "deleted":
            for r in rows:
                await db.delete(r)
        elif action in {"suspend", "unsuspend", "new_permissions_accepted"}:
            for r in rows:
                fields = gh.installation_fields(inst)
                r.suspended = action == "suspend" or (action != "unsuspend" and fields["suspended"])
                r.permissions = fields["permissions"] or r.permissions
        await db.commit()
        gh._TOKENS.clear()
    return {"ok": True}
