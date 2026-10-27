from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

log = logging.getLogger("assistant")


class Base(DeclarativeBase):
    pass


engine = create_async_engine(get_settings().async_database_url, pool_size=20, max_overflow=20, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    from app import models  # noqa: F401  (register tables)

    cfg = get_settings()
    last: Exception | None = None
    for attempt in range(1, 16):
        try:
            await _migrate()
            return
        except Exception as vis_exc:  # noqa: BLE001
            last = vis_exc
            msg = str(vis_exc)
            log.warning(
                "database not ready host=%s attempt=%s/%s err=%s",
                cfg.database_host,
                attempt,
                15,
                vis_exc,
            )
            dns = "Name or service not known" in msg or "Temporary failure in name resolution" in msg
            if dns and attempt >= 3:
                break
            await asyncio.sleep(2 if dns else min(8, attempt))
    raise RuntimeError(
        f"Could not reach Postgres at {cfg.database_host}. "
        "On Render, DATABASE_HOSTPORT must be the checkme-postgres hostport, not Docker host 'postgres'. "
        f"{last}"
    ) from last


async def _migrate() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE connectors ADD COLUMN IF NOT EXISTS description TEXT DEFAULT ''"))
        await conn.execute(text("ALTER TABLE inbox_items ADD COLUMN IF NOT EXISTS to_header VARCHAR(1000) DEFAULT ''"))
        await conn.execute(text("ALTER TABLE inbox_items ADD COLUMN IF NOT EXISTS cc_header VARCHAR(1000) DEFAULT ''"))
        await conn.execute(text("ALTER TABLE inbox_items ADD COLUMN IF NOT EXISTS recipient_role VARCHAR(12) DEFAULT 'to'"))
        await conn.execute(
            text("UPDATE users SET timezone = 'Asia/Kolkata' WHERE timezone IS NULL OR timezone IN ('UTC', 'utc', '')")
        )
        await conn.execute(text("ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS agent_id UUID"))
        await conn.execute(text("ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS group_id UUID"))
        await conn.execute(text("ALTER TABLE workspace_agents ADD COLUMN IF NOT EXISTS pinned BOOLEAN DEFAULT false"))
        await conn.execute(text("ALTER TABLE workspace_agents ADD COLUMN IF NOT EXISTS archived BOOLEAN DEFAULT false"))
        await conn.execute(text("ALTER TABLE workspace_agents ADD COLUMN IF NOT EXISTS unread BOOLEAN DEFAULT false"))
        await conn.execute(text("ALTER TABLE agent_groups ADD COLUMN IF NOT EXISTS outcome TEXT DEFAULT ''"))
        await conn.execute(text("ALTER TABLE agent_groups ADD COLUMN IF NOT EXISTS owner_id UUID"))
        await conn.execute(text("ALTER TABLE memory_passages ADD COLUMN IF NOT EXISTS agent_id UUID"))
