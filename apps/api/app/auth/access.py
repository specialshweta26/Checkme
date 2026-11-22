"""Who may create a new Lumio account (docs/ROLLOUT.md "Sign-ups").

SIGNUP_MODE is open (anyone), invite (only the emails and domains in SIGNUP_ALLOW) or closed (no one new). It only
gates creating an account: everyone who already has one signs in as before. SSO connections are set up by the
operator for a company, so people arriving through one are not gated here.
"""

from __future__ import annotations

from app.config import get_settings

CLOSED_MESSAGE = "Lumio isn't open for new sign-ups yet. Ask the team for an invite."


def _allowed() -> set[str]:
    raw = get_settings().signup_allow or ""
    return {x.strip().lower().lstrip("@") for x in raw.replace(";", ",").replace("\n", ",").split(",") if x.strip()}


def mode() -> str:
    m = (get_settings().signup_mode or "open").strip().lower()
    return m if m in ("open", "invite", "closed") else "invite"  # a typo fails safe


def may_sign_up(email: str) -> bool:
    m = mode()
    if m == "open":
        return True
    if m == "closed":
        return False
    email = (email or "").strip().lower()
    if "@" not in email:
        return False
    allowed = _allowed()
    return email in allowed or email.rsplit("@", 1)[1] in allowed


async def may_sign_up_db(db, email: str) -> bool:
    """may_sign_up, plus the invites an admin added in the console (signup_invites) while sign-ups are invite-only."""
    if may_sign_up(email):
        return True
    if mode() != "invite" or db is None:
        return False
    email = (email or "").strip().lower()
    if "@" not in email:
        return False
    from sqlalchemy import select

    from app.models import SignupInvite

    hit = (await db.execute(select(SignupInvite.id).where(SignupInvite.value.in_([email, email.rsplit("@", 1)[1]])))).first()
    if hit is not None:
        return True
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func

    from app.models import TeamInvite

    # An open invitation to a team (docs/TEAMS.md) also lets that address sign up.
    week = datetime.now(timezone.utc) - timedelta(days=7)
    team = (await db.execute(select(TeamInvite.id).where(func.lower(TeamInvite.email) == email, TeamInvite.accepted_at.is_(None),
                                                         TeamInvite.created_at >= week))).first()  # fmt: skip
    return team is not None


SUSPENDED_MESSAGE = "This account is suspended. Contact the Lumio team."


def is_admin(user) -> bool:
    raw = get_settings().admin_emails or ""
    admins = {x.strip().lower() for x in raw.replace(";", ",").split(",") if x.strip()}
    return bool(user is not None and (getattr(user, "email", "") or "").strip().lower() in admins
                and getattr(user, "suspended_at", None) is None)  # fmt: skip
