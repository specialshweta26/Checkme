from __future__ import annotations

import asyncio
import logging
import re
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


class _GuardedConn:
    """Runs migration statements without locking tables that need no change.

    `ALTER TABLE … ADD COLUMN IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS` take a table lock in Postgres even when
    the column or index is already there. During a deploy the old instance is still serving, and one of its open
    transactions makes that lock wait, and every other query on the table then queues behind the waiting ALTER: the
    live app stalls. So statements whose column/index already exists are skipped, and the rest run under a short
    lock_timeout (init_db retries) instead of waiting indefinitely."""

    _ADD_COLUMN = re.compile(r"^\s*ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (\w+)", re.I)
    _CREATE_INDEX = re.compile(r"^\s*CREATE (?:UNIQUE )?INDEX IF NOT EXISTS (\w+)", re.I)

    def __init__(self, conn, columns: set[tuple[str, str]], indexes: set[str]):
        self._conn = conn
        self._columns = columns
        self._indexes = indexes
        self.skipped = 0

    async def execute(self, statement, *args, **kwargs):
        sql = str(statement)
        m = self._ADD_COLUMN.match(sql)
        if m and (m.group(1).lower(), m.group(2).lower()) in self._columns:
            self.skipped += 1
            return None
        m = self._CREATE_INDEX.match(sql)
        if m and m.group(1).lower() in self._indexes:
            self.skipped += 1
            return None
        return await self._conn.execute(statement, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


async def _existing_schema(conn) -> tuple[set[tuple[str, str]], set[str]]:
    cols = await conn.execute(text("SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public'"))
    idx = await conn.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"))
    return {(t.lower(), c.lower()) for t, c in cols.all()}, {i.lower() for (i,) in idx.all()}


async def _migrate() -> None:
    async with engine.begin() as raw:
        # Several API instances can start at once: one migrates at a time (released when this transaction ends).
        await raw.execute(text("SELECT pg_advisory_xact_lock(7316402)"))
        # Never wait long for a table lock behind live traffic: fail, and init_db tries again shortly.
        await raw.execute(text("SET LOCAL lock_timeout = '5s'"))
        await raw.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await raw.run_sync(Base.metadata.create_all)
        columns, indexes = await _existing_schema(raw)
        conn = _GuardedConn(raw, columns, indexes)
        await conn.execute(text("ALTER TABLE connectors ADD COLUMN IF NOT EXISTS description TEXT DEFAULT ''"))
        await conn.execute(text("ALTER TABLE inbox_items ADD COLUMN IF NOT EXISTS to_header VARCHAR(1000) DEFAULT ''"))
        await conn.execute(text("ALTER TABLE inbox_items ADD COLUMN IF NOT EXISTS cc_header VARCHAR(1000) DEFAULT ''"))
        await conn.execute(text("ALTER TABLE inbox_items ADD COLUMN IF NOT EXISTS recipient_role VARCHAR(12) DEFAULT 'to'"))
        await conn.execute(text("ALTER TABLE inbox_items ADD COLUMN IF NOT EXISTS ai_draft TEXT DEFAULT ''"))
        await conn.execute(text("ALTER TABLE inbox_items ADD COLUMN IF NOT EXISTS key_person BOOLEAN DEFAULT false"))
        await conn.execute(text("ALTER TABLE inbox_items ADD COLUMN IF NOT EXISTS sender_note VARCHAR(400) DEFAULT ''"))
        await conn.execute(
            text("UPDATE users SET timezone = 'Asia/Kolkata' WHERE timezone IS NULL OR timezone IN ('UTC', 'utc', '')")
        )
        await conn.execute(text("ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS agent_id UUID"))
        await conn.execute(text("ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS group_id UUID"))
        # site_logins is a new table: create_all above makes it.
        for column in ("proposed_steps JSONB", "proposed_note JSONB", "proposed_at TIMESTAMPTZ", "prior_steps JSONB",
                       "run_key_hash VARCHAR(64) DEFAULT ''"):
            await conn.execute(text(f"ALTER TABLE browser_traces ADD COLUMN IF NOT EXISTS {column}"))
        await conn.execute(
            text(
                "ALTER TABLE scheduled_tasks ADD COLUMN IF NOT EXISTS "
                "chat_session_id UUID REFERENCES chat_sessions(id) ON DELETE SET NULL"
            )
        )
        await conn.execute(text("ALTER TABLE scheduled_tasks ADD COLUMN IF NOT EXISTS interval_minutes INTEGER"))
        await conn.execute(text("ALTER TABLE workspace_agents ADD COLUMN IF NOT EXISTS pinned BOOLEAN DEFAULT false"))
        await conn.execute(text("ALTER TABLE workspace_agents ADD COLUMN IF NOT EXISTS archived BOOLEAN DEFAULT false"))
        await conn.execute(text("ALTER TABLE workspace_agents ADD COLUMN IF NOT EXISTS unread BOOLEAN DEFAULT false"))
        await conn.execute(text("ALTER TABLE workspace_agents ADD COLUMN IF NOT EXISTS focus VARCHAR(40) DEFAULT ''"))
        await conn.execute(text("ALTER TABLE workspace_agents ADD COLUMN IF NOT EXISTS avatar TEXT DEFAULT ''"))
        await conn.execute(text("ALTER TABLE agent_groups ADD COLUMN IF NOT EXISTS outcome TEXT DEFAULT ''"))
        await conn.execute(text("ALTER TABLE agent_groups ADD COLUMN IF NOT EXISTS owner_id UUID"))
        await conn.execute(text("ALTER TABLE automations ADD COLUMN IF NOT EXISTS trigger_type VARCHAR(20) DEFAULT 'schedule'"))
        await conn.execute(text("ALTER TABLE automations ADD COLUMN IF NOT EXISTS webhook_secret_hash VARCHAR(64)"))
        for ddl in (
            "status VARCHAR(20) DEFAULT 'ready'",
            "error TEXT DEFAULT ''",
            "transcript JSONB DEFAULT '[]'::jsonb",
            "frames JSONB DEFAULT '[]'::jsonb",
            "notes JSONB DEFAULT '[]'::jsonb",
            "auto_named BOOLEAN DEFAULT false",
            "updated_at TIMESTAMPTZ",
        ):
            await conn.execute(text(f"ALTER TABLE agent_skills ADD COLUMN IF NOT EXISTS {ddl}"))
        await _migrate_github(conn)
        await _migrate_jobs(conn)
        await _migrate_agent_scope(conn)
        await _migrate_memory(conn)
        await _migrate_tool_policy(conn)
        await _migrate_user_memory(conn)
        await conn.execute(text("ALTER TABLE agent_skills ADD COLUMN IF NOT EXISTS revision INTEGER DEFAULT 1"))


async def _migrate_jobs(conn) -> None:
    """agent_jobs / job_events come from create_all; keep later columns and indexes in step."""
    for stmt in (
        "ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS scheduled_task_id UUID",
        "ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS attempts INTEGER DEFAULT 0",
        "ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ",
        "CREATE INDEX IF NOT EXISTS ix_job_events_root_id_id ON job_events (root_id, id)",
        "CREATE INDEX IF NOT EXISTS ix_agent_jobs_user_status ON agent_jobs (user_id, status)",
        "ALTER TABLE IF EXISTS run_checkpoints DROP COLUMN IF EXISTS writes",  # now run_checkpoint_writes
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


USER_MEMORY_LABELS = ("voice", "prefs", "learned", "people", "work")


async def _once(conn, name: str) -> bool:
    """True the first time `name` is seen (across all instances and restarts); used for one-time data changes."""
    await conn.execute(text("CREATE TABLE IF NOT EXISTS app_migrations (name VARCHAR(120) PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())"))
    got = await conn.execute(
        text("INSERT INTO app_migrations (name) VALUES (:n) ON CONFLICT (name) DO NOTHING RETURNING name"), {"n": name}
    )
    return got.first() is not None


async def _migrate_user_memory(conn) -> None:
    """Memory belongs to the user, shared by all of their agents (docs/LLD.md "Memory").

    Columns: claim fields on memory_passages. Once: per-agent facts become user-level, and each agent's voice/prefs/
    learned/people/work notes merge into the user's single copy on their default agent. Always: one active copy of
    each fact per user, enforced by unique indexes so concurrent turns can't store it twice."""
    for ddl in (
        "subject_entity_id BIGINT REFERENCES memory_entities(id) ON DELETE SET NULL",
        "predicate VARCHAR(80) DEFAULT ''",
        "trust VARCHAR(20) DEFAULT 'user_said'",
        "confidence DOUBLE PRECISION DEFAULT 0.8",
        "reinforced INTEGER DEFAULT 1",
        "valid_from TIMESTAMPTZ",
        "valid_to TIMESTAMPTZ",
        "last_confirmed_at TIMESTAMPTZ",
    ):
        await conn.execute(text(f"ALTER TABLE memory_passages ADD COLUMN IF NOT EXISTS {ddl}"))
    for ddl in (
        "mail_in INTEGER DEFAULT 0",
        "mail_out INTEGER DEFAULT 0",
        "meetings INTEGER DEFAULT 0",
        "last_interaction_at TIMESTAMPTZ",
        "summary TEXT DEFAULT ''",
        "card_built_at TIMESTAMPTZ",
    ):
        await conn.execute(text(f"ALTER TABLE memory_entities ADD COLUMN IF NOT EXISTS {ddl}"))
    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_memory_passages_user_status_scope ON memory_passages (user_id, status, scope)"
    ))
    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_memory_passages_user_subject_predicate "
        "ON memory_passages (user_id, subject_entity_id, predicate)"
    ))
    if await _once(conn, "user_level_memory_v1"):
        await conn.execute(text(
            "UPDATE memory_passages SET scope = 'user' WHERE kind = 'archival' AND scope IN ('agent', 'user_shared')"
        ))
        await conn.execute(text("UPDATE memory_passages SET valid_from = created_at WHERE valid_from IS NULL"))
        # User-level facts live on the user's default agent, so deleting another agent (rows cascade) keeps them.
        await conn.execute(text(
            """
            UPDATE memory_passages p SET agent_id = d.id
            FROM workspace_agents d
            WHERE d.user_id = p.user_id AND d.kind = 'default' AND p.scope IN ('user', 'user_shared') AND p.agent_id <> d.id
            """
        ))
        rows = (await conn.execute(text(
            """
            SELECT b.user_id, b.label, b.value, d.id AS default_id
            FROM memory_blocks b
            JOIN workspace_agents a ON a.id = b.agent_id
            JOIN workspace_agents d ON d.user_id = b.user_id AND d.kind = 'default'
            WHERE b.label = ANY(:labels) AND a.kind <> 'default' AND coalesce(b.value, '') <> ''
            ORDER BY b.user_id, b.label, b.updated_at
            """
        ), {"labels": list(USER_MEMORY_LABELS)})).all()
        for user_id, label, value, default_id in rows:
            current = (await conn.execute(text(
                "SELECT value FROM memory_blocks WHERE user_id = :u AND agent_id = :a AND label = :l"
            ), {"u": user_id, "a": default_id, "l": label})).scalar()
            lines = [ln for ln in (current or "").splitlines() if ln.strip()]
            seen = {ln.strip().lower() for ln in lines}
            lines += [ln for ln in value.splitlines() if ln.strip() and ln.strip().lower() not in seen]
            await conn.execute(text(
                "INSERT INTO memory_blocks (user_id, agent_id, label, value, updated_at) VALUES (:u, :a, :l, :v, now()) "
                "ON CONFLICT (user_id, agent_id, label) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"
            ), {"u": user_id, "a": default_id, "l": label, "v": "\n".join(lines)})
        await conn.execute(text(
            """
            DELETE FROM memory_blocks b USING workspace_agents a
            WHERE a.id = b.agent_id AND a.kind <> 'default' AND b.label = ANY(:labels)
            """
        ), {"labels": list(USER_MEMORY_LABELS)})
    # One active copy of each fact: keep the newest, mark older duplicates superseded, then enforce it.
    await conn.execute(text(
        """
        UPDATE memory_passages p SET status = 'superseded'
        FROM (
          SELECT id, row_number() OVER (
            PARTITION BY user_id,
                         CASE WHEN scope = 'agent' THEN 'agent' ELSE 'user' END,
                         CASE WHEN scope = 'agent' THEN agent_id END,
                         md5(lower(text))
            ORDER BY id DESC) AS rn
          FROM memory_passages WHERE status = 'active'
        ) d
        WHERE p.id = d.id AND d.rn > 1
        """
    ))
    await conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_passages_user_fact ON memory_passages (user_id, md5(lower(text))) "
        "WHERE status = 'active' AND scope IN ('user', 'user_shared')"
    ))
    await conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_passages_agent_note ON memory_passages (user_id, agent_id, md5(lower(text))) "
        "WHERE status = 'active' AND scope = 'agent'"
    ))


async def _migrate_tool_policy(conn) -> None:
    """MCP tool rows written before policy 1 were "safe unless named delete/send". Mark them needs-approval
    (policy_v 2) once; the startup re-sync rewrites them from each server's own read-only annotations."""
    await conn.execute(text("ALTER TABLE tool_index ADD COLUMN IF NOT EXISTS policy_v INTEGER DEFAULT 0"))
    await conn.execute(
        text(
            "UPDATE tool_index SET read_only = false, destructive = true, policy_v = 2 "
            "WHERE server_key LIKE 'mcp:%' AND (policy_v IS NULL OR policy_v = 0)"
        )
    )
    await conn.execute(text("UPDATE tool_index SET policy_v = 1 WHERE policy_v IS NULL OR policy_v = 0"))


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
