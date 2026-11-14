"""Saved sign-ins for the browser (docs/BROWSING.md "Saved sign-ins").

The user adds a site's username and password on the Saved runs page; they never pass through chat or a model.
Both are encrypted with the app key (``app.security.encrypt``). When a browser task or saved run on that site
needs them, code adds ``login_username`` / ``login_password`` to the run's values: the planner sees only the
``{{names}}``, the daemon types the values, and results and logs are scrubbed. CAPTCHAs and one-time codes still go
to the user.

Access: each sign-in lists the agents allowed to use it (``agent_ids``). Only those agents get its values, see it in
``browser_logins``, or may open its site at all in the shared browser (``gate``): the browser's cookies are shared, so
an agent left out must not reach a session another agent signed in to. A sign-in saved before access existed starts
with the agents whose own skills mention its site; nobody else is added until the user says so in the Vault.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from app.security import decrypt, encrypt

log = logging.getLogger(__name__)

USERNAME, PASSWORD = "login_username", "login_password"
MAX_VALUE = 500


def normalize_host(raw: str) -> str:
    """'https://www.Example.com/login' or 'example.com' -> 'example.com'."""
    from app.agent.fast_browser import host_of

    text = str(raw or "").strip()
    if not text:
        return ""
    return host_of(text if "://" in text else f"https://{text}")


def hint(username: str) -> str:
    """Enough of a username to recognise it, never the whole thing: 'g•••@gmail.com', 'ad•••'."""
    name = str(username or "")
    if "@" in name:
        local, _, domain = name.partition("@")
        return f"{local[:1]}•••@{domain}"
    return f"{name[:2]}•••" if name else ""


def _host_matches(saved: str, host: str) -> bool:
    """A login for example.com also covers its subdomains (app.example.com), never the other way round."""
    return bool(saved and host) and (host == saved or host.endswith("." + saved))


def _row_out(row) -> dict:
    return {
        "id": str(row.id), "host": row.host, "label": row.label or row.host, "username_hint": hint(decrypt(row.username_enc) or ""),
        "agent_ids": list(row.agent_ids or []),
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }  # fmt: skip


def allowed(row, agent_id) -> bool:
    """May this agent use this sign-in? No agent (an unattributed run) never may."""
    return bool(agent_id) and str(agent_id) in {str(a) for a in (getattr(row, "agent_ids", None) or [])}


def skill_mentions(host: str, skill) -> bool:
    """Does a skill work on this site (in its instructions, recorded steps or source)?"""
    import json

    blob = " ".join(
        [str(getattr(skill, "instruction", "") or ""), json.dumps(getattr(skill, "steps", None) or [], default=str), json.dumps(getattr(skill, "source", None) or {}, default=str)]
    ).lower()
    return bool(host) and host.lower() in blob


async def _settle_access(db, user_id, rows) -> None:
    """Sign-ins saved before per-agent access: allow the agents whose own skills (not shared ones) mention the site."""
    todo = [r for r in rows if r.agent_ids is None]
    if not todo:
        return
    from sqlalchemy import select

    from app.models import AgentSkill

    skills = (await db.execute(select(AgentSkill).where(AgentSkill.user_id == uuid.UUID(str(user_id)), AgentSkill.agent_id.is_not(None)))).scalars().all()
    for row in todo:
        row.agent_ids = sorted({str(s.agent_id) for s in skills if skill_mentions(row.host, s)})
    await db.commit()


async def list_logins(user_id) -> list[dict]:
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import SiteLogin

    async with SessionLocal() as db:
        q = select(SiteLogin).where(SiteLogin.user_id == uuid.UUID(str(user_id))).order_by(SiteLogin.host)
        rows = (await db.execute(q)).scalars().all()
        await _settle_access(db, user_id, rows)
        return [_row_out(r) for r in rows]


async def save_login(user_id, host: str, username: str, password: str, label: str = "", agent_ids: list | None = None) -> dict:
    """Add or replace the sign-in for a site. An empty password keeps the saved one (rename / new username).
    ``agent_ids``: who may use it (a new sign-in with none given is usable by no agent until the user picks some)."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import SiteLogin

    site = normalize_host(host)
    if not site or "." not in site:
        raise ValueError("give the site address, for example portal.example.com")
    username = str(username or "").strip()
    if not username or len(username) > MAX_VALUE or len(str(password or "")) > MAX_VALUE:
        raise ValueError("a username (and password) of at most 500 characters is needed")
    uid = uuid.UUID(str(user_id))
    async with SessionLocal() as db:
        row = (await db.execute(select(SiteLogin).where(SiteLogin.user_id == uid, SiteLogin.host == site))).scalars().first()
        if row is None:
            if not password:
                raise ValueError("a password is needed for a new sign-in")
            row = SiteLogin(user_id=uid, host=site, agent_ids=[])
            db.add(row)
        if agent_ids is not None:
            row.agent_ids = await _own_agents(db, uid, agent_ids)
        row.username_enc = encrypt(username)
        if password:
            row.password_enc = encrypt(str(password))
        row.label = str(label or "").strip()[:200]
        await db.commit()
        _RULES.pop(str(user_id), None)
        return _row_out(row)


async def _own_agents(db, user_id, agent_ids: list) -> list[str]:
    """Only the user's own agents can be given a sign-in."""
    from sqlalchemy import select

    from app.models import WorkspaceAgent

    wanted = set()
    for a in agent_ids or []:
        try:
            wanted.add(uuid.UUID(str(a)))
        except (TypeError, ValueError):
            raise ValueError("unknown agent") from None
    if not wanted:
        return []
    mine = (await db.execute(select(WorkspaceAgent.id).where(WorkspaceAgent.user_id == uuid.UUID(str(user_id)), WorkspaceAgent.id.in_(wanted)))).scalars().all()
    if len(mine) != len(wanted):
        raise ValueError("unknown agent")
    return sorted(str(a) for a in mine)


async def set_access(user_id, login_id: str, agent_ids: list) -> dict | None:
    """Change which agents may use a sign-in."""
    from app.db import SessionLocal
    from app.models import SiteLogin

    async with SessionLocal() as db:
        try:
            row = await db.get(SiteLogin, uuid.UUID(str(login_id)))
        except (TypeError, ValueError):
            return None
        if row is None or str(row.user_id) != str(user_id):
            return None
        row.agent_ids = await _own_agents(db, row.user_id, agent_ids)
        await db.commit()
        _RULES.pop(str(user_id), None)
        return _row_out(row)


async def delete_login(user_id, login_id: str) -> bool:
    from app.db import SessionLocal
    from app.models import SiteLogin

    async with SessionLocal() as db:
        try:
            row = await db.get(SiteLogin, uuid.UUID(str(login_id)))
        except (TypeError, ValueError):
            return False
        if row is None or str(row.user_id) != str(user_id):
            return False
        await db.delete(row)
        await db.commit()
        _RULES.pop(str(user_id), None)
        return True


async def values_for(user_id, host: str, agent_id=None) -> dict[str, str]:
    """{login_username, login_password} for the saved sign-in that covers this site, if this agent may use it, or {}."""
    site = normalize_host(host)
    if user_id is None or not site or not agent_id:
        return {}
    try:
        from sqlalchemy import select

        from app.db import SessionLocal
        from app.models import SiteLogin

        async with SessionLocal() as db:
            rows = (await db.execute(select(SiteLogin).where(SiteLogin.user_id == uuid.UUID(str(user_id))))).scalars().all()
            await _settle_access(db, user_id, rows)
            hits = sorted((r for r in rows if _host_matches(r.host, site)), key=lambda r: len(r.host), reverse=True)
            if not hits or not allowed(hits[0], agent_id):
                return {}
            row = hits[0]
            username, password = decrypt(row.username_enc), decrypt(row.password_enc)
            if not username or not password:
                return {}
            row.last_used_at = datetime.now(timezone.utc)
            await db.commit()
            return {USERNAME: username, PASSWORD: password}
    except Exception as exc:  # noqa: BLE001
        log.info("saved sign-in lookup skipped: %s", type(exc).__name__)
        return {}


async def sites_with_logins(user_id) -> list[str]:
    try:
        return [r["host"] for r in await list_logins(user_id)]
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------- the browser gate

import time as _time

_RULES: dict[str, tuple[float, list[dict]]] = {}
RULES_TTL = 15.0


async def rules(user_id) -> list[dict]:
    """[{host, label, agent_ids}] for this user's sign-ins, cached briefly (the gate runs on every browser command)."""
    key = str(user_id)
    hit = _RULES.get(key)
    if hit and hit[0] > _time.monotonic():
        return hit[1]
    try:
        out = [{"host": r["host"], "label": r["label"], "agent_ids": r["agent_ids"]} for r in await list_logins(user_id)]
    except Exception as exc:  # noqa: BLE001 - fail closed only for sites we know; an outage must not stop all browsing
        log.info("sign-in rules unavailable: %s", type(exc).__name__)
        return hit[1] if hit else []
    if len(_RULES) > 2000:
        _RULES.clear()
    _RULES[key] = (_time.monotonic() + RULES_TTL, out)
    return out


def blocked_rule(rows: list[dict], url: str, agent_id) -> dict | None:
    """The sign-in rule that keeps this agent off this address, or None."""
    site = normalize_host(url) if url else ""
    if not site:
        return None
    hits = sorted((r for r in rows if _host_matches(r["host"], site)), key=lambda r: len(r["host"]), reverse=True)
    if not hits:
        return None
    rule = hits[0]
    return None if agent_id and str(agent_id) in {str(a) for a in rule.get("agent_ids") or []} else rule


def refusal(rule: dict, agent_name: str = "") -> dict:
    who = agent_name or "This agent"
    return {
        "ok": False,
        "blocked": "vault",
        "error": f"{who} isn't allowed to use the saved sign-in for {rule['host']}, so it can't open that site. "
        f"Tell the user: they can allow {who} in Profile > Vault ({rule.get('label') or rule['host']}), or ask an agent that has access. "
        "Do not try another way in.",
    }


async def gate(user_id, agent, url: str) -> dict | None:
    """A refusal if this agent may not open this address (a site with a saved sign-in it wasn't given), else None."""
    if user_id is None or not url:
        return None
    rule = blocked_rule(await rules(user_id), url, getattr(agent, "id", None))
    return refusal(rule, getattr(agent, "name", "") or "") if rule else None
