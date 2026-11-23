"""A user's own model keys and the model they picked (docs/LLD.md "Model gateway", phase 2).

Keys are encrypted and never leave the server: the page gets the provider, label, last four characters, check status
and model list. A choice names one saved key and one model for a purpose ("agent" for the agent's replies, "small"
for background work), either as the user's default (scope "user") or for one agent (scope = its id, phase 3).
``load_all_choices`` turns them into ModelSpecs for app.llm_gateway at the start of a run.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import llm_gateway
from app.model_keys import providers
from app.models import ModelChoice, ModelKey, WorkspaceAgent
from app.security import decrypt, encrypt

PURPOSES = ("agent", "small")
MAX_KEYS = 20
MAX_KEY_CHARS = 400


class ModelKeyError(ValueError):
    """Refused, with a message safe to show."""


def public_key(row: ModelKey) -> dict[str, Any]:
    p = providers.PROVIDERS.get(row.provider)
    return {
        "id": str(row.id),
        "provider": row.provider,
        "provider_name": p.name if p else row.provider,
        "runs": bool(p and p.runs),
        "label": row.label or (p.name if p else row.provider),
        "hint": row.hint,
        "base_url": row.base_url,
        "status": row.status,
        "status_note": row.status_note,
        "models": list(row.models or []),
        "checked_at": row.checked_at.isoformat() if row.checked_at else None,
    }


async def _keys(db: AsyncSession, user_id) -> list[ModelKey]:
    q = select(ModelKey).where(ModelKey.user_id == user_id).order_by(ModelKey.created_at)
    return list((await db.execute(q)).scalars().all())


async def _key(db: AsyncSession, user_id, key_id: str) -> ModelKey:
    try:
        kid = uuid.UUID(str(key_id))
    except ValueError as exc:
        raise ModelKeyError("That key isn't saved.") from exc
    row = (await db.execute(select(ModelKey).where(ModelKey.id == kid, ModelKey.user_id == user_id))).scalar_one_or_none()
    if row is None:
        raise ModelKeyError("That key isn't saved.")
    return row


async def _choices(db: AsyncSession, user_id, scope: str | None = "user") -> list[ModelChoice]:
    """One scope's choices, or every scope's with ``scope=None``."""
    q = select(ModelChoice).where(ModelChoice.user_id == user_id)
    if scope is not None:
        q = q.where(ModelChoice.scope == scope)
    return list((await db.execute(q)).scalars().all())


async def _scope(db: AsyncSession, user, agent_id: str | None) -> str:
    """"user", or the id of one of this user's agents."""
    if not agent_id:
        return "user"
    try:
        aid = uuid.UUID(str(agent_id))
    except ValueError as exc:
        raise ModelKeyError("That agent isn't yours.") from exc
    found = (await db.execute(select(WorkspaceAgent.id).where(WorkspaceAgent.id == aid, WorkspaceAgent.user_id == user.id))).scalar_one_or_none()
    if found is None:
        raise ModelKeyError("That agent isn't yours.")
    return str(aid)


async def overview(db: AsyncSession, user) -> dict[str, Any]:
    keys = await _keys(db, user.id)
    chosen: dict[str, dict] = {}
    agents: dict[str, dict] = {}
    for c in await _choices(db, user.id, None):
        (chosen if c.scope == "user" else agents.setdefault(c.scope, {}))[c.purpose] = {"key_id": str(c.key_id), "model": c.model}
    return {
        "providers": providers.public(),
        "keys": [public_key(k) for k in keys],
        "choices": chosen,
        "agents": agents,
        "lumio": {"agent": llm_gateway.resolve(None, "agent").model, "small": llm_gateway.resolve(None, "small").model},
    }


async def _checked_base(provider: providers.Provider, base_url: str) -> str:
    if not provider.custom_url:
        return ""
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise ModelKeyError("Add the service's API address, for example https://api.mistral.ai/v1.")
    from app.connectors.urlsafety import UnsafeUrl, validate_public_url

    try:
        await validate_public_url(url)
    except UnsafeUrl as exc:
        raise ModelKeyError(f"That address can't be used: {exc}.") from exc
    return url


async def _run_check(row: ModelKey, api_key: str) -> None:
    try:
        row.models = await providers.check(row.provider, api_key, row.base_url)
        row.status, row.status_note = "ok", ""
    except providers.KeyCheckFailed as exc:
        row.status, row.status_note = "failed", str(exc)
    row.checked_at = datetime.now(timezone.utc)


async def add_key(db: AsyncSession, user, provider: str, api_key: str, label: str = "", base_url: str = "") -> dict:
    try:
        p = providers.get(provider)
    except providers.KeyCheckFailed as exc:
        raise ModelKeyError(str(exc)) from exc
    key = (api_key or "").strip()
    if not key:
        raise ModelKeyError("Paste the API key.")
    if len(key) > MAX_KEY_CHARS or any(ch.isspace() for ch in key):
        raise ModelKeyError("That doesn't look like an API key.")
    if len(await _keys(db, user.id)) >= MAX_KEYS:
        raise ModelKeyError(f"You can save up to {MAX_KEYS} keys. Remove one first.")
    row = ModelKey(user_id=user.id, provider=p.key, label=(label or "").strip()[:80], key_enc=encrypt(key),
                   hint=key[-4:], base_url=await _checked_base(p, base_url))  # fmt: skip
    await _run_check(row, key)
    if row.status != "ok":
        raise ModelKeyError(row.status_note)  # a key that doesn't work isn't saved
    db.add(row)
    await db.commit()
    return public_key(row)


async def recheck(db: AsyncSession, user, key_id: str) -> dict:
    row = await _key(db, user.id, key_id)
    key = decrypt(row.key_enc)
    if not key:
        row.status, row.status_note = "failed", "Lumio can't read this key any more. Remove it and add it again."
    else:
        await _run_check(row, key)
    await db.commit()
    return public_key(row)


async def remove_key(db: AsyncSession, user, key_id: str) -> None:
    row = await _key(db, user.id, key_id)
    await db.execute(delete(ModelChoice).where(ModelChoice.key_id == row.id))
    await db.delete(row)
    await db.commit()


async def choose(db: AsyncSession, user, purpose: str, key_id: str | None, model: str = "", agent_id: str | None = None) -> dict:
    """Use ``key_id`` and ``model`` for ``purpose``, as the user's default or for one agent. No key_id removes the
    choice: the user's default goes back to Lumio's model, an agent goes back to the user's default."""
    if purpose not in PURPOSES:
        raise ModelKeyError("Pick the agent model or the background model.")
    scope = await _scope(db, user, agent_id)
    existing = next((c for c in await _choices(db, user.id, scope) if c.purpose == purpose), None)
    if not key_id:
        if existing is not None:
            await db.delete(existing)
            await db.commit()
        return await overview(db, user)
    row = await _key(db, user.id, key_id)
    p = providers.PROVIDERS.get(row.provider)
    if not (p and p.runs):
        raise ModelKeyError(f"Lumio can't run agents on {p.name if p else row.provider} yet. The key is saved for when it can.")
    if row.status != "ok":
        raise ModelKeyError("This key failed its last check. Check it again first.")
    name = (model or "").strip()
    if not name:
        raise ModelKeyError("Pick a model.")
    if row.models and name not in row.models:
        raise ModelKeyError(f"This key can't use {name}. Pick one from its list, or check the key again.")
    if existing is None:
        db.add(ModelChoice(user_id=user.id, scope=scope, purpose=purpose, key_id=row.id, model=name[:160]))
    else:
        existing.key_id, existing.model = row.id, name[:160]
    await db.commit()
    return await overview(db, user)


async def load_all_choices(db: AsyncSession, user_id) -> dict[str, dict[str, llm_gateway.ModelSpec]]:
    """Every choice the user made as specs, {scope: {purpose: spec}}, for one run. A key Lumio can't read or run is
    left out (that purpose falls back to the next choice, then Lumio's model)."""
    choices = await _choices(db, user_id, None)
    if not choices:
        return {}
    keys = {k.id: k for k in await _keys(db, user_id)}
    out: dict[str, dict[str, llm_gateway.ModelSpec]] = {}
    for c in choices:
        row = keys.get(c.key_id)
        p = providers.PROVIDERS.get(row.provider) if row else None
        secret = decrypt(row.key_enc) if row else None
        if row is None or not (p and p.runs) or not secret:
            continue
        out.setdefault(c.scope, {})[c.purpose] = llm_gateway.ModelSpec(
            provider=row.provider, model=c.model, api_key=secret, base_url=row.base_url or "", purpose=c.purpose,
            source="user" if c.scope == "user" else "agent")  # fmt: skip
    return out


async def load_choices(db: AsyncSession, user_id, agent_id=None) -> dict[str, llm_gateway.ModelSpec]:
    """The choices one agent (or the user's default, with no agent) runs with."""
    return llm_gateway.for_agent(await load_all_choices(db, user_id), agent_id)
