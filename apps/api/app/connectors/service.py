from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.platform import PLATFORM, sync_platform
from app.connectors.registry import mark_synced, server_key_for, spec_from_mcp, upsert_tool_index
from app.connectors.virtual import VIRTUAL_MAIL_TOOLS
from app.db import SessionLocal
from app.models import Account, Connector, User
from connector_core import MAIL_CALENDAR_TOOLS

log = logging.getLogger(__name__)

BUILTIN_META = {
    "gmail": {"name": "Gmail", "slug": "gmail", "tags": ["mail", "calendar", "files"]},
    "outlook": {"name": "Outlook", "slug": "outlook", "tags": ["mail", "calendar", "files"]},
}


async def ensure_builtin_connector(db: AsyncSession, user: User, account: Account) -> Connector:
    provider = "gmail" if account.provider == "google" else "outlook"
    meta = BUILTIN_META[provider]
    row = (
        await db.execute(
            select(Connector).where(Connector.user_id == user.id, Connector.slug == meta["slug"])
        )
    ).scalar_one_or_none()
    if row is None:
        row = Connector(
            user_id=user.id,
            kind="builtin",
            provider=provider,
            account_id=account.id,
            name=meta["name"],
            slug=meta["slug"],
            auth_type="provider",
            server_key=server_key_for("builtin", provider, None),
            tags=meta["tags"],
            default_for=["mail", "calendar"],
            enabled=True,
            health="pending",
        )
        db.add(row)
        await db.flush()
    else:
        row.account_id = account.id
        row.health = "pending"
    return row


async def sync_builtin(db: AsyncSession, connector: Connector) -> None:
    provider = connector.provider
    if provider in PLATFORM:
        await sync_platform(db, connector)
        return
    meta = BUILTIN_META[provider]
    specs = list(MAIL_CALENDAR_TOOLS) + list(VIRTUAL_MAIL_TOOLS)
    await upsert_tool_index(db, connector.server_key or server_key_for("builtin", provider, None), meta["name"], specs)
    connector.server_key = connector.server_key or server_key_for("builtin", provider, None)
    connector.tags = meta["tags"]
    await mark_synced(db, connector)


async def sync_mcp(db: AsyncSession, connector: Connector) -> None:
    from app.auth.tokens import mcp_access_token
    from app.connectors.mcp_client import list_mcp_tools
    from app.security import decrypt_json

    creds = decrypt_json(connector.credentials_enc)
    if connector.auth_type == "oauth":
        creds["access_token"] = await mcp_access_token(connector.id)
    tools = await list_mcp_tools(connector.server_url, creds)
    specs = [spec_from_mcp(t) for t in tools]
    key = connector.server_key or server_key_for("mcp", "mcp", connector.server_url)
    await upsert_tool_index(db, key, connector.name, specs)
    connector.server_key = key
    await mark_synced(db, connector)


async def sync_connector(db: AsyncSession, connector: Connector) -> None:
    try:
        if connector.kind == "builtin":
            await sync_builtin(db, connector)
        else:
            await sync_mcp(db, connector)
    except Exception as exc:  # noqa: BLE001
        log.warning("sync failed for %s: %s", connector.id, exc)
        connector.health = "down"
        connector.health_detail = str(exc)[:500]
        await db.commit()
        raise


async def sync_in_background(connector_id: uuid.UUID, _ignore: bool = False) -> None:
    async with SessionLocal() as db:
        row = await db.get(Connector, connector_id)
        if row is None:
            return
        try:
            await sync_connector(db, row)
        except Exception:  # noqa: BLE001
            pass


async def next_slug(db: AsyncSession, user_id: uuid.UUID, base: str) -> str:
    base = "".join(c for c in base.lower() if c.isalnum() or c == "-")[:32] or "mcp"
    existing = set(
        (await db.execute(select(Connector.slug).where(Connector.user_id == user_id))).scalars().all()
    )
    if base not in existing:
        return base
    for i in range(2, 50):
        cand = f"{base}-{i}"
        if cand not in existing:
            return cand
    return f"{base}-{uuid.uuid4().hex[:6]}"
