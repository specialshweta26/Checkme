from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(get_settings().async_database_url, pool_size=20, max_overflow=20, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    from app import models  # noqa: F401  (register tables)

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
