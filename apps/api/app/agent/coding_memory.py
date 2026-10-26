"""Per-agent coding memory: the user's repos, default repo, branch conventions and past PRs.

Stored as one core memory block (label "coding") on the coding agent itself (per-agent
memory, PR #7), so the Coder does not ask again which repo or branch style to use. The
block is plain "key: value" lines so it reads well in the system prompt.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

LABEL = "coding"
KEYS = ("default_repo", "repos", "branch_convention", "base_branch", "recent_prs", "notes")
MAX_REPOS = 8
MAX_PRS = 6
MAX_CHARS = 790


def parse(value: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (value or "").splitlines():
        key, sep, val = line.partition(":")
        key = key.strip().lower().replace(" ", "_")
        if sep and key in KEYS:
            out[key] = val.strip()
    return out


def render(data: dict[str, str]) -> str:
    lines = [f"{k}: {data[k]}" for k in KEYS if data.get(k)]
    text = "\n".join(lines)
    while len(text) > MAX_CHARS and data.get("recent_prs"):
        prs = data["recent_prs"].split(" ; ")
        data["recent_prs"] = " ; ".join(prs[1:])
        text = "\n".join(f"{k}: {data[k]}" for k in KEYS if data.get(k))
    return text[:MAX_CHARS]


def _push_front(items: str, item: str, cap: int, sep: str = ", ") -> str:
    rows = [r for r in (items or "").split(sep) if r.strip() and r.strip().lower() != item.lower()]
    return sep.join([item, *rows][:cap])


def update(data: dict[str, str], *, repo: str = "", pr: dict | None = None, prefs: dict | None = None) -> dict[str, str]:
    data = dict(data)
    if repo:
        data["repos"] = _push_front(data.get("repos", ""), repo, MAX_REPOS)
        data.setdefault("default_repo", repo)
    if pr and pr.get("url"):
        label = f"{pr.get('repo', '')}#{pr.get('number', '?')} {pr.get('branch', '')} {pr.get('state', 'open')} {pr['url']}".strip()
        rows = [r for r in (data.get("recent_prs") or "").split(" ; ") if r.strip() and pr["url"] not in r]
        data["recent_prs"] = " ; ".join([*rows, label][-MAX_PRS:])
    for key, val in (prefs or {}).items():
        if key in KEYS and str(val or "").strip():
            data[key] = str(val).strip()[:200]
    return data


async def read(db, user, agent) -> str:
    if db is None or agent is None:
        return ""
    from app.agent.memory import read_core

    try:
        return await read_core(db, user, LABEL, agent=agent)
    except Exception as exc:  # noqa: BLE001
        log.debug("coding memory read failed: %s", exc)
        return ""


async def note(ctx, *, repo: str = "", pr: dict | None = None, prefs: dict | None = None) -> dict | None:
    """Merge facts into the agent's coding block. Uses its own DB session so it never commits the turn's work."""
    agent = getattr(ctx, "agent", None)
    if getattr(ctx, "db", None) is None or agent is None or getattr(agent, "id", None) is None:
        return None
    try:
        from app.agent.memory import read_core, upsert_core
        from app.db import SessionLocal

        async with SessionLocal() as db:
            current = parse(await read_core(db, ctx.user, LABEL, agent=agent))
            merged = update(current, repo=repo, pr=pr, prefs=prefs)
            if merged != current:
                await upsert_core(db, ctx.user.id, LABEL, render(merged), agent.id)
            return merged
    except Exception as exc:  # noqa: BLE001
        log.debug("coding memory write failed: %s", exc)
        return None
