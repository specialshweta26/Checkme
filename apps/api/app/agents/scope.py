"""Which workspace agent owns legacy per-user memory and routines.

The startup migration inserts a Chief row when a user has unscoped data, then
applies ``backfill_statements``. ``assign_missing`` is the same rule in memory
so tests can lock it without Postgres.
"""

from __future__ import annotations

SCOPED_TABLES = ("memory_blocks", "memory_passages", "scheduled_tasks", "automations")

PROMOTE_NAMED_CHIEF_SQL = """
UPDATE workspace_agents wa
SET kind = 'default'
WHERE wa.name = 'Chief'
  AND wa.kind IS DISTINCT FROM 'default'
  AND NOT EXISTS (
    SELECT 1 FROM workspace_agents other
    WHERE other.user_id = wa.user_id AND other.kind = 'default'
  )
"""

INSERT_MISSING_CHIEF_SQL = """
INSERT INTO workspace_agents (
  id, user_id, name, title, instructions, color, kind,
  pinned, archived, unread, created_at, updated_at
)
SELECT gen_random_uuid(), u.id, 'Chief', 'chief of staff', '', '#f97316', 'default',
       false, false, false, now(), now()
FROM users u
WHERE NOT EXISTS (
  SELECT 1 FROM workspace_agents wa
  WHERE wa.user_id = u.id AND (wa.kind = 'default' OR wa.name = 'Chief')
)
AND (
  EXISTS (SELECT 1 FROM memory_blocks mb WHERE mb.user_id = u.id AND mb.agent_id IS NULL)
  OR EXISTS (SELECT 1 FROM memory_passages mp WHERE mp.user_id = u.id AND mp.agent_id IS NULL)
  OR EXISTS (SELECT 1 FROM scheduled_tasks st WHERE st.user_id = u.id AND st.agent_id IS NULL)
  OR EXISTS (SELECT 1 FROM automations au WHERE au.user_id = u.id AND au.agent_id IS NULL)
  OR (u.letta_agent_id IS NOT NULL AND btrim(u.letta_agent_id) <> '')
)
ON CONFLICT (user_id, name) DO NOTHING
"""

COPY_CHIEF_LETTA_SQL = """
UPDATE workspace_agents wa
SET letta_agent_id = u.letta_agent_id
FROM users u
WHERE wa.user_id = u.id
  AND wa.kind = 'default'
  AND (wa.letta_agent_id IS NULL OR btrim(wa.letta_agent_id) = '')
  AND u.letta_agent_id IS NOT NULL
  AND btrim(u.letta_agent_id) <> ''
"""


def choose_default_agent(agents: list[dict]):
    """Prefer ``kind=default``. Otherwise the agent named Chief. Matches ``ensure_default_agent``."""
    from app.agents.service import DEFAULT_AGENT_NAME

    if not agents:
        return None
    for row in agents:
        if (row.get("kind") or "") == "default":
            return row
    for row in agents:
        if row.get("name") == DEFAULT_AGENT_NAME:
            return row
    return None


def owner_by_user(agents: list[dict]) -> dict[str, str]:
    grouped: dict[str, list[dict]] = {}
    for row in agents:
        grouped.setdefault(str(row.get("user_id")), []).append(row)
    owners: dict[str, str] = {}
    for user_id, rows in grouped.items():
        chosen = choose_default_agent(rows)
        if chosen is not None and chosen.get("id"):
            owners[user_id] = str(chosen["id"])
    return owners


def assign_missing(rows: list[dict], owners: dict[str, str]) -> list[dict]:
    """Fill empty ``agent_id`` from that user's default agent. Never overwrite."""
    assigned: list[dict] = []
    for row in rows:
        current = row.get("agent_id")
        if current:
            assigned.append({**row, "agent_id": str(current)})
            continue
        owner = owners.get(str(row.get("user_id")))
        assigned.append({**row, "agent_id": str(owner) if owner else None})
    return assigned


def backfill_statements(owners: dict[str, str]) -> list[tuple[str, dict]]:
    """SQL updates that apply ``assign_missing`` on each scoped table."""
    statements: list[tuple[str, dict]] = []
    for table in SCOPED_TABLES:
        for user_id, agent_id in owners.items():
            statements.append(
                (
                    f"UPDATE {table} SET agent_id = CAST(:aid AS uuid) "
                    f"WHERE user_id = CAST(:uid AS uuid) AND agent_id IS NULL",
                    {"uid": str(user_id), "aid": str(agent_id)},
                )
            )
    return statements
