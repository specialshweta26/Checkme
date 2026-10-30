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
        await conn.execute(text("ALTER TABLE workspace_agents ADD COLUMN IF NOT EXISTS focus VARCHAR(40) DEFAULT ''"))
        await conn.execute(text("ALTER TABLE agent_groups ADD COLUMN IF NOT EXISTS outcome TEXT DEFAULT ''"))
        await conn.execute(text("ALTER TABLE agent_groups ADD COLUMN IF NOT EXISTS owner_id UUID"))
        await conn.execute(text("ALTER TABLE automations ADD COLUMN IF NOT EXISTS trigger_type VARCHAR(20) DEFAULT 'schedule'"))
        await conn.execute(text("ALTER TABLE automations ADD COLUMN IF NOT EXISTS webhook_secret_hash VARCHAR(64)"))
        await _migrate_github(conn)
        await _migrate_jobs(conn)
        await _migrate_agent_scope(conn)
        await _migrate_memory(conn)


async def _migrate_jobs(conn) -> None:
    """agent_jobs / job_events come from create_all; keep later columns and indexes in step."""
    for stmt in (
        "ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS scheduled_task_id UUID",
        "ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS attempts INTEGER DEFAULT 0",
        "ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ",
        "CREATE INDEX IF NOT EXISTS ix_job_events_root_id_id ON job_events (root_id, id)",
        "CREATE INDEX IF NOT EXISTS ix_agent_jobs_user_status ON agent_jobs (user_id, status)",
    ):
        await conn.execute(text(stmt))


async def _migrate_github(conn) -> None:
    """Legacy GitHub App installations table (unused since GitHub moved to Composio; kept, never dropped)."""
    await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS coder_seeded BOOLEAN DEFAULT false"))
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS github_installations (
                id UUID PRIMARY KEY,
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                installation_id BIGINT NOT NULL,
                account_login VARCHAR(100) DEFAULT '',
                account_type VARCHAR(20) DEFAULT '',
                repository_selection VARCHAR(20) DEFAULT '',
                permissions JSONB DEFAULT '{}'::jsonb,
                suspended BOOLEAN DEFAULT false,
                created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now()
            )
            """
        )
    )
    await conn.execute(text("ALTER TABLE github_installations ADD COLUMN IF NOT EXISTS suspended BOOLEAN DEFAULT false"))
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_github_installations_user_inst "
            "ON github_installations (user_id, installation_id)"
        )
    )
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_github_installations_installation_id ON github_installations (installation_id)")
    )


async def _pk_columns(conn, table: str) -> list[str]:
    if table not in {"memory_blocks"}:
        raise ValueError(table)
    result = await conn.execute(
        text(
            f"""
            SELECT a.attname
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
            WHERE i.indrelid = '{table}'::regclass AND i.indisprimary
            ORDER BY a.attnum
            """
        )
    )
    return [row[0] for row in result]


async def _constraint_exists(conn, name: str) -> bool:
    result = await conn.execute(text("SELECT 1 FROM pg_constraint WHERE conname = :name"), {"name": name})
    return result.first() is not None


async def _add_agent_fk(conn, table: str, name: str) -> None:
    if table not in {"memory_blocks", "memory_passages", "scheduled_tasks", "automations"}:
        raise ValueError(table)
    if await _constraint_exists(conn, name):
        return
    existing = await conn.execute(
        text(
            """
            SELECT 1
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY (c.conkey)
            WHERE t.relname = :table AND c.contype = 'f' AND a.attname = 'agent_id'
            """
        ),
        {"table": table},
    )
    if existing.first():
        return
    await conn.execute(
        text(
            f"ALTER TABLE {table} ADD CONSTRAINT {name} "
            f"FOREIGN KEY (agent_id) REFERENCES workspace_agents(id) ON DELETE CASCADE"
        )
    )


async def _migrate_agent_scope(conn) -> None:
    """Add per-agent columns and assign existing rows to each user's Chief."""
    from app.agents.scope import (
        COPY_CHIEF_LETTA_SQL,
        INSERT_MISSING_CHIEF_SQL,
        PROMOTE_NAMED_CHIEF_SQL,
        backfill_statements,
        owner_by_user,
    )

    await conn.execute(text("ALTER TABLE workspace_agents ADD COLUMN IF NOT EXISTS letta_agent_id VARCHAR(100)"))
    await conn.execute(text("ALTER TABLE memory_blocks ADD COLUMN IF NOT EXISTS agent_id UUID"))
    await conn.execute(text("ALTER TABLE memory_passages ADD COLUMN IF NOT EXISTS agent_id UUID"))
    await conn.execute(text("ALTER TABLE scheduled_tasks ADD COLUMN IF NOT EXISTS agent_id UUID"))
    await conn.execute(text("ALTER TABLE automations ADD COLUMN IF NOT EXISTS agent_id UUID"))
    await conn.execute(text(PROMOTE_NAMED_CHIEF_SQL))
    await conn.execute(text(INSERT_MISSING_CHIEF_SQL))
    agent_rows = (
        await conn.execute(text("SELECT id::text AS id, user_id::text AS user_id, name, kind FROM workspace_agents"))
    ).mappings().all()
    owners = owner_by_user([dict(row) for row in agent_rows])
    for statement, params in backfill_statements(owners):
        await conn.execute(text(statement), params)
    await conn.execute(text(COPY_CHIEF_LETTA_SQL))

    for table in ("memory_passages", "memory_blocks"):
        leftover = (
            await conn.execute(text(f"SELECT count(*) FROM {table} WHERE agent_id IS NULL"))
        ).scalar() or 0
        if leftover:
            log.warning("dropping %s %s rows with no default agent to assign", leftover, table)
            await conn.execute(text(f"DELETE FROM {table} WHERE agent_id IS NULL"))

    pk_cols = await _pk_columns(conn, "memory_blocks")
    if "agent_id" not in pk_cols:
        await conn.execute(text("ALTER TABLE memory_blocks DROP CONSTRAINT IF EXISTS memory_blocks_pkey"))
        await conn.execute(text("ALTER TABLE memory_blocks ALTER COLUMN agent_id SET NOT NULL"))
        await conn.execute(text("ALTER TABLE memory_blocks ADD PRIMARY KEY (user_id, agent_id, label)"))

    await conn.execute(text("ALTER TABLE memory_passages ALTER COLUMN agent_id SET NOT NULL"))
    for table in ("scheduled_tasks", "automations"):
        remaining = (await conn.execute(text(f"SELECT count(*) FROM {table} WHERE agent_id IS NULL"))).scalar() or 0
        if remaining:
            log.warning("%s rows still missing agent_id: %s", table, remaining)
            continue
        await conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN agent_id SET NOT NULL"))

    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_memory_passages_user_agent ON memory_passages (user_id, agent_id)")
    )
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_scheduled_tasks_agent_id ON scheduled_tasks (agent_id)"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_automations_agent_id ON automations (agent_id)"))
    await _add_agent_fk(conn, "memory_blocks", "memory_blocks_agent_id_fkey")
    await _add_agent_fk(conn, "memory_passages", "memory_passages_agent_id_fkey")
    await _add_agent_fk(conn, "scheduled_tasks", "scheduled_tasks_agent_id_fkey")
    await _add_agent_fk(conn, "automations", "automations_agent_id_fkey")


async def _migrate_memory(conn) -> None:
    """ChatGPT-style per-agent memory: scope, status, provenance, full-text, extract jobs."""
    await conn.execute(text("ALTER TABLE memory_blocks ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now()"))
    await conn.execute(text("ALTER TABLE memory_passages ADD COLUMN IF NOT EXISTS scope VARCHAR(20) DEFAULT 'agent'"))
    await conn.execute(text("ALTER TABLE memory_passages ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'active'"))
    await conn.execute(text("ALTER TABLE memory_passages ADD COLUMN IF NOT EXISTS source_session_id UUID"))
    await conn.execute(text("ALTER TABLE memory_passages ADD COLUMN IF NOT EXISTS source_message_id BIGINT"))
    await conn.execute(text("ALTER TABLE memory_passages ADD COLUMN IF NOT EXISTS supersedes_id BIGINT"))
    await conn.execute(text("UPDATE memory_passages SET scope = 'agent' WHERE scope IS NULL OR btrim(scope) = ''"))
    await conn.execute(text("UPDATE memory_passages SET status = 'active' WHERE status IS NULL OR btrim(status) = ''"))
    tsv = await conn.execute(
        text(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'memory_passages' AND column_name = 'search_tsv'
            """
        )
    )
    if tsv.first() is None:
        await conn.execute(
            text(
                """
                ALTER TABLE memory_passages
                ADD COLUMN search_tsv tsvector
                GENERATED ALWAYS AS (to_tsvector('simple', coalesce(text, ''))) STORED
                """
            )
        )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_memory_passages_user_agent_status "
            "ON memory_passages (user_id, agent_id, status)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_memory_passages_user_shared "
            "ON memory_passages (user_id, status) WHERE scope = 'user_shared'"
        )
    )
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_memory_passages_tsv ON memory_passages USING gin (search_tsv)"))
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_chat_messages_user_request ON chat_messages (user_id, request_id)")
    )
