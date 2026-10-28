from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.config import get_settings
from app.db import Base

DIMS = get_settings().embedding_dims


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320))
    display_name: Mapped[str] = mapped_column(String(200), default="")
    primary_provider: Mapped[str] = mapped_column(String(20))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    letta_agent_id: Mapped[str | None] = mapped_column(String(100))
    coder_seeded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Account(Base):
    """An OAuth identity (Google or Microsoft). Used for sign-in and for the built-in mail connector."""

    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("provider", "subject"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(20))
    subject: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(320))
    refresh_token_enc: Mapped[str | None] = mapped_column(Text)
    scopes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    user_agent: Mapped[str] = mapped_column(String(300), default="")


class Connector(Base):
    __tablename__ = "connectors"
    __table_args__ = (UniqueConstraint("user_id", "slug"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # builtin | mcp
    provider: Mapped[str] = mapped_column(String(20))  # gmail | outlook | mcp
    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(40))
    alias: Mapped[str | None] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    server_url: Mapped[str | None] = mapped_column(Text)
    transport: Mapped[str] = mapped_column(String(20), default="streamable_http")
    auth_type: Mapped[str] = mapped_column(String(20), default="provider")  # provider | oauth | bearer | header | none
    credentials_enc: Mapped[str | None] = mapped_column(Text)
    server_key: Mapped[str | None] = mapped_column(String(300), index=True)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    default_for: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    health: Mapped[str] = mapped_column(String(20), default="pending")  # pending | ok | auth_expired | down
    health_detail: Mapped[str] = mapped_column(Text, default="")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ServerCard(Base):
    """Shared per tool-set (not per user): capability card, groups, core tools."""

    __tablename__ = "server_cards"
    server_key: Mapped[str] = mapped_column(String(300), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    card: Mapped[str] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    groups: Mapped[dict] = mapped_column(JSONB, default=dict)  # {group: {"label": str, "tools": [names]}}
    core_tools: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    tool_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ToolIndex(Base):
    """Shared tool registry row. Embeddings are computed once per tool-set and reused by every user."""

    __tablename__ = "tool_index"
    __table_args__ = (
        UniqueConstraint("server_key", "tool_name"),
        Index("ix_tool_index_tsv", "search_tsv", postgresql_using="gin"),
        Index(
            "ix_tool_index_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    server_key: Mapped[str] = mapped_column(String(300), index=True)
    tool_name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text)
    group: Mapped[str] = mapped_column(String(80))
    input_schema: Mapped[dict] = mapped_column(JSONB)
    read_only: Mapped[bool] = mapped_column(Boolean, default=True)
    destructive: Mapped[bool] = mapped_column(Boolean, default=False)
    requires: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    produces: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    search_text: Mapped[str] = mapped_column(Text)
    search_tsv = mapped_column(TSVECTOR, Computed("to_tsvector('simple', search_text)", persisted=True))
    embedding = mapped_column(Vector(DIMS), nullable=True)


class ToolUsage(Base):
    __tablename__ = "tool_usage"
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    connector_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tool_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    success: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failure: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class ToolEdge(Base):
    """Tools that tend to run in sequence (shared per tool-set)."""

    __tablename__ = "tool_edges"
    server_key: Mapped[str] = mapped_column(String(300), primary_key=True)
    from_tool: Mapped[str] = mapped_column(String(128), primary_key=True)
    to_tool: Mapped[str] = mapped_column(String(128), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)


class WorkspaceAgent(Base):
    """A named bot the user can chat with. Groups of these can talk to each other."""

    __tablename__ = "workspace_agents"
    __table_args__ = (UniqueConstraint("user_id", "name"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(160), default="")
    instructions: Mapped[str] = mapped_column(Text, default="")
    # Focus picked on the agent's first screen: email_calendar | coding | research | errands | "" (see agents/focus.py)
    focus: Mapped[str] = mapped_column(String(40), default="")
    color: Mapped[str] = mapped_column(String(16), default="#f97316")
    kind: Mapped[str] = mapped_column(String(20), default="custom")  # default | custom
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    unread: Mapped[bool] = mapped_column(Boolean, default=False)
    letta_agent_id: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class GitHubInstallation(Base):
    """Legacy: a GitHub App installation from the removed GitHub App path. Kept as history; no longer read or written."""

    __tablename__ = "github_installations"
    __table_args__ = (UniqueConstraint("user_id", "installation_id"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    installation_id: Mapped[int] = mapped_column(BigInteger, index=True)
    account_login: Mapped[str] = mapped_column(String(100), default="")
    account_type: Mapped[str] = mapped_column(String(20), default="")  # User | Organization
    repository_selection: Mapped[str] = mapped_column(String(20), default="")  # all | selected
    permissions: Mapped[dict] = mapped_column(JSONB, default=dict)
    suspended: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class DeployCredential(Base):
    """A user's CLI credential for one deploy platform (Render, Vercel, Fly, …), Fernet-encrypted.

    Entered on the Connectors page only; the agent gets env names, never values (app/agent/deploy.py)."""

    __tablename__ = "deploy_credentials"
    __table_args__ = (UniqueConstraint("user_id", "platform"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(20))
    credentials_enc: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class AgentConnector(Base):
    """When rows exist for a connector, only those agents may use it. No rows = every agent."""

    __tablename__ = "agent_connectors"
    __table_args__ = (UniqueConstraint("agent_id", "connector_id"),)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspace_agents.id", ondelete="CASCADE"), primary_key=True)
    connector_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("connectors.id", ondelete="CASCADE"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AgentToolkit(Base):
    """When rows exist for a Composio toolkit, only those agents may use it. No rows = every agent."""

    __tablename__ = "agent_toolkits"
    __table_args__ = (UniqueConstraint("agent_id", "toolkit"),)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspace_agents.id", ondelete="CASCADE"), primary_key=True)
    toolkit: Mapped[str] = mapped_column(String(80), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AgentGroup(Base):
    __tablename__ = "agent_groups"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    outcome: Mapped[str] = mapped_column(Text, default="")
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class AgentGroupMember(Base):
    __tablename__ = "agent_group_members"
    __table_args__ = (UniqueConstraint("group_id", "agent_id"),)
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_groups.id", ondelete="CASCADE"), primary_key=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspace_agents.id", ondelete="CASCADE"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="New chat")
    state: Mapped[dict] = mapped_column(JSONB, default=dict)  # working memory
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    group_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (Index("ix_chat_messages_session", "session_id", "id"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat_sessions.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    request_id: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ThreadSummary(Base):
    __tablename__ = "thread_summaries"
    connector_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(300), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    last_message_id: Mapped[str] = mapped_column(String(300))
    summary: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class RouteLog(Base):
    __tablename__ = "route_logs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    path: Mapped[str] = mapped_column(String(30))
    candidates: Mapped[list] = mapped_column(JSONB, default=list)
    chosen: Mapped[dict] = mapped_column(JSONB, default=dict)
    outcome: Mapped[str] = mapped_column(String(30), default="")
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class MemoryBlock(Base):
    """Fallback core memory when Letta is not configured. One row per agent and label."""

    __tablename__ = "memory_blocks"
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspace_agents.id", ondelete="CASCADE"), primary_key=True
    )
    label: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Commitment(Base):
    __tablename__ = "commitments"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    connector_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    text: Mapped[str] = mapped_column(Text)
    counterparty: Mapped[str] = mapped_column(String(320), default="")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="open")
    message_id: Mapped[str] = mapped_column(String(300), default="")
    thread_id: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AdminItem(Base):
    __tablename__ = "admin_items"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    kind: Mapped[str] = mapped_column(String(40))  # receipt | invoice | bill | subscription
    vendor: Mapped[str] = mapped_column(String(200), default="")
    amount: Mapped[str] = mapped_column(String(40), default="")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message_id: Mapped[str] = mapped_column(String(300), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Automation(Base):
    __tablename__ = "automations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspace_agents.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    trigger: Mapped[str] = mapped_column(Text)
    trigger_type: Mapped[str] = mapped_column(String(20), default="schedule")
    webhook_secret_hash: Mapped[str | None] = mapped_column(String(64))
    condition: Mapped[str] = mapped_column(Text, default="")
    action: Mapped[str] = mapped_column(Text)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class WebhookDelivery(Base):
    """Idempotency record connecting an inbound webhook delivery to its agent job."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (UniqueConstraint("automation_id", "event_id"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    automation_id: Mapped[int] = mapped_column(ForeignKey("automations.id", ondelete="CASCADE"), index=True)
    event_id: Mapped[str] = mapped_column(String(255))
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_jobs.id", ondelete="CASCADE"), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ScheduledTask(Base):
    __tablename__ = "scheduled_tasks"
    __table_args__ = (Index("ix_scheduled_tasks_due", "status", "next_run_at"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspace_agents.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(200))
    instruction: Mapped[str] = mapped_column(Text)
    prompt: Mapped[str] = mapped_column(Text, default="")
    schedule_kind: Mapped[str] = mapped_column(String(20), default="once")
    schedule_label: Mapped[str] = mapped_column(String(120), default="")
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_result: Mapped[str] = mapped_column(Text, default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="active")
    connected_apps: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    run_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class AgentJob(Base):
    """A background agent run (coding work, delegated sub-tasks, scheduled runs). See app/jobs."""

    __tablename__ = "agent_jobs"
    __table_args__ = (Index("ix_agent_jobs_status_created", "status", "created_at"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspace_agents.id", ondelete="SET NULL"), index=True
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="SET NULL"), index=True
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_jobs.id", ondelete="CASCADE"), index=True
    )
    root_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    depth: Mapped[int] = mapped_column(Integer, default=1)
    role: Mapped[str] = mapped_column(String(20), default="coder")  # planner | coder | reviewer | worker
    kind: Mapped[str] = mapped_column(String(20), default="chat")  # chat | delegate | scheduled
    title: Mapped[str] = mapped_column(String(200), default="")
    prompt: Mapped[str] = mapped_column(Text, default="")
    # queued | running | waiting_approval | waiting_children | done | failed | cancelled
    status: Mapped[str] = mapped_column(String(20), default="queued")
    result: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    scheduled_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("scheduled_tasks.id", ondelete="SET NULL")
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class JobEvent(Base):
    """Timeline entries for a job tree: steps, plans, diffs, test output, PR links, approvals, results."""

    __tablename__ = "job_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_jobs.id", ondelete="CASCADE"), index=True)
    root_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    agent_name: Mapped[str] = mapped_column(String(80), default="")
    # step | plan | diff | test | pr | message | approval | result | error | status
    kind: Mapped[str] = mapped_column(String(20), default="step")
    text: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class MemoryPassage(Base):
    """Fallback archival + episodic memory when Letta is not configured."""

    __tablename__ = "memory_passages"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspace_agents.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(20))  # archival | episodic
    text: Mapped[str] = mapped_column(Text)
    embedding = mapped_column(Vector(DIMS), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AgentSkill(Base):
    """A saved or taught workflow for one bot. Teaching records the screen plus optional, user-started narration."""

    __tablename__ = "agent_skills"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    name: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(20), default="saved")  # saved | taught
    steps: Mapped[list] = mapped_column(JSONB, default=list)
    instruction: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # Taught skills are saved while recording: recording -> writing -> ready. ``error`` holds a failed write-up.
    status: Mapped[str] = mapped_column(String(20), default="ready")
    error: Mapped[str] = mapped_column(Text, default="")
    transcript: Mapped[list] = mapped_column(JSONB, default=list)  # narration segments {t, t_end, text}
    frames: Mapped[list] = mapped_column(JSONB, default=list)  # key screenshots {t, jpeg (base64)}
    notes: Mapped[list] = mapped_column(JSONB, default=list)
    auto_named: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InboxItem(Base):
    """Triaged inbox card: classification, thread-aware draft, and user action state."""

    __tablename__ = "inbox_items"
    __table_args__ = (UniqueConstraint("user_id", "message_id"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    connector_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    message_id: Mapped[str] = mapped_column(String(300))
    thread_id: Mapped[str] = mapped_column(String(300), default="")
    subject: Mapped[str] = mapped_column(String(500), default="")
    sender: Mapped[str] = mapped_column(String(400), default="")
    to_header: Mapped[str] = mapped_column(String(1000), default="")
    cc_header: Mapped[str] = mapped_column(String(1000), default="")
    recipient_role: Mapped[str] = mapped_column(String(12), default="to")
    received_at: Mapped[str] = mapped_column(String(80), default="")
    snippet: Mapped[str] = mapped_column(Text, default="")
    classification: Mapped[str] = mapped_column(String(20), default="")  # reply | respond | discard
    summary: Mapped[str] = mapped_column(Text, default="")
    key_points: Mapped[list] = mapped_column(JSONB, default=list)
    draft: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | sent | discarded
    unread: Mapped[bool] = mapped_column(Boolean, default=True)
    seen: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class ResearchPage(Base):
    """One user's cached reading of a public page (research memory, reused across jobs while fresh)."""

    __tablename__ = "research_pages"
    __table_args__ = (UniqueConstraint("user_id", "canonical_url", name="uq_research_pages_user_url"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    canonical_url: Mapped[str] = mapped_column(String(2000))
    url: Mapped[str] = mapped_column(String(2000))
    title: Mapped[str] = mapped_column(String(300), default="")
    text: Mapped[str] = mapped_column(Text, default="")  # readable text, clipped
    note: Mapped[str] = mapped_column(Text, default="")  # last extractive note
    links: Mapped[list] = mapped_column(JSONB, default=list)  # [[url, anchor], ...] for re-crawling without a fetch
    goal: Mapped[str] = mapped_column(String(500), default="")  # goal of the crawl that last read it
    embedding = mapped_column(Vector(DIMS), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class ResearchSummary(Base):
    """What a research crawl or job concluded, searchable by later jobs of the same user."""

    __tablename__ = "research_summaries"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    crawl_id: Mapped[str] = mapped_column(String(40), default="")
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    goal: Mapped[str] = mapped_column(String(500), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    sources: Mapped[list] = mapped_column(JSONB, default=list)
    embedding = mapped_column(Vector(DIMS), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
