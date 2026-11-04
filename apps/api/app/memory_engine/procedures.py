"""Immutable skill revisions. The live skill row is the latest; a task pins the revision it started."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def add_version(session, row, *, bump: bool) -> int:
    revision = int(getattr(row, "revision", None) or 1)
    if bump:
        revision += 1
        row.revision = revision
    else:
        row.revision = revision
    from app.models import AgentSkillVersion

    session.add(
        AgentSkillVersion(
            skill_id=row.id,
            user_id=row.user_id,
            revision=revision,
            name=row.name or "",
            kind=row.kind or "saved",
            steps=list(row.steps or []),
            instruction=row.instruction or "",
        )
    )
    return revision
