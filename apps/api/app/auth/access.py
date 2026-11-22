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
