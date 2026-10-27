"""Provision Daytona computer and Composio marketplace connectors for a user."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.connectors.composio_box import COMPOSIO_TOOLS
from app.connectors.daytona_box import DAYTONA_TOOLS
from app.connectors.github import GITHUB_TOOLS, LEGACY_SLUG
from app.connectors.github import PROVIDER as GITHUB_PROVIDER
from app.connectors.github import SLUG as GITHUB_SLUG
from app.connectors.registry import mark_synced, server_key_for, upsert_tool_index
from app.models import Connector, User

log = logging.getLogger(__name__)

PLATFORM = {
    "daytona": {
        "name": "Computer",
        "slug": "computer",
        "tags": ["computer", "sandbox", "shell"],
        "description": "This user's live computer — shell, files, and a persistent Chromium tab the chat can show.",
        "tools": DAYTONA_TOOLS,
    },
    "composio": {
        "name": "Composio",
        "slug": "composio",
        "tags": ["marketplace", "apps"],
        "description": "Composio app marketplace. Search and run tools from connected apps, or connect a new app.",
        "tools": COMPOSIO_TOOLS,
    },
    GITHUB_PROVIDER: {
        "name": "GitHub",
        "slug": GITHUB_SLUG,
        "tags": ["github", "code", "pull requests", "issues"],
        "description": (
            "GitHub through the user's Composio GitHub connection: explore any repo they can reach (branches, tree, "
            "files, code search, history, diffs, blame), PRs and issues; comment, review and request reviewers; open, "
            "update, push and merge only with approval."
        ),
        "tools": GITHUB_TOOLS,
    },
}
# Rows the removed GitHub App created keep working (runtime routes them to Composio) until replaced.
PLATFORM["github_app"] = PLATFORM[GITHUB_PROVIDER]


def platform_specs(provider: str):
    meta = PLATFORM.get(provider)
    return list(meta["tools"]) if meta else []


def platform_needs_sync(row: Connector | None) -> bool:
    if row is None or row.health != "ok" or not row.last_synced_at:
        return True
    ts = row.last_synced_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - ts > timedelta(hours=6)


async def ensure_platform_connectors(db: AsyncSession, user: User) -> list[Connector]:
    s = get_settings()
    wanted: list[tuple[str, bool]] = [
        ("daytona", bool(s.daytona_api_key)),
        ("composio", bool(s.composio_api_key)),
        # Always offered with Composio; each call checks the user's ACTIVE GitHub connection and says how to connect.
        (GITHUB_PROVIDER, bool(s.composio_api_key)),
    ]
    await _retire_github_app(db, user)
    out: list[Connector] = []
    for provider, enabled in wanted:
        if not enabled:
            continue
        meta = PLATFORM[provider]
        row = (
            await db.execute(select(Connector).where(Connector.user_id == user.id, Connector.slug == meta["slug"]))
        ).scalar_one_or_none()
        if row is None:
            row = Connector(
                user_id=user.id,
                kind="builtin",
                provider=provider,
                name=meta["name"],
                slug=meta["slug"],
                description=meta["description"],
                auth_type="platform",
                server_key=server_key_for("builtin", provider, None),
                tags=meta["tags"],
                default_for=[provider],
                enabled=True,
                health="pending",
            )
            db.add(row)
            await db.flush()
        else:
            row.provider = provider
            row.kind = "builtin"
            row.auth_type = "platform"
            row.server_key = row.server_key or server_key_for("builtin", provider, None)
            if not row.description:
                row.description = meta["description"]
        if platform_needs_sync(row):
            try:
                await sync_platform(db, row)
            except Exception as vis_exc:  # noqa: BLE001
                log.warning("platform sync failed for %s: %s", provider, vis_exc)
                row.health = "down"
                row.health_detail = str(vis_exc)[:500]
                await db.commit()
        out.append(row)
    return out


async def _retire_github_app(db: AsyncSession, user: User) -> None:
    """Turn off the old GitHub App connector row. Nothing is deleted (github_installations stays as history)."""
    try:
        old = (
            await db.execute(
                select(Connector).where(
                    Connector.user_id == user.id, Connector.slug == LEGACY_SLUG, Connector.kind == "builtin"
                )
            )
        ).scalar_one_or_none()
        if old is not None and old.enabled:
            old.enabled = False
            old.health_detail = "Replaced by GitHub through Composio (Connectors → GitHub)."
            await db.flush()
    except Exception as vis_exc:  # noqa: BLE001
        log.warning("retiring the GitHub App connector failed: %s", vis_exc)


async def sync_platform(db: AsyncSession, connector: Connector) -> None:
    meta = PLATFORM.get(connector.provider)
    if meta is None:
        raise ValueError(f"unknown platform provider {connector.provider}")
    key = connector.server_key or server_key_for("builtin", connector.provider, None)
    await upsert_tool_index(db, key, meta["name"], list(meta["tools"]))
    connector.server_key = key
    connector.tags = meta["tags"]
    await mark_synced(db, connector)
