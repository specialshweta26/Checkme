"""Per-user research memory: page notes keyed by canonical URL (with fetch time and embeddings) and summaries.

Every query is scoped by ``user_id``; nothing is shared between users. Later crawls reuse a page while it is fresh
instead of fetching it again, and agents can search what earlier jobs read and concluded.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from sqlalchemy import or_, select, text

log = logging.getLogger(__name__)

TEXT_MAX = 12000
DEFAULT_MAX_AGE_H = 72.0


def _dims() -> int:
    from app.config import get_settings

    return int(get_settings().embedding_dims)


def _vec_ok(vec) -> bool:
    return bool(vec) and len(vec) == _dims()


def _vec_lit(vec) -> str:
    return "[" + ",".join(f"{float(x):.7f}" for x in vec) + "]"


class ResearchStore(Protocol):
    async def fresh_pages(self, canonicals: list[str], max_age_h: float) -> dict[str, dict]: ...

    async def put_pages(self, rows: list[dict]) -> None: ...

    async def search(self, query: str, vec: list[float] | None, top: int) -> dict: ...

    async def save_summary(self, row: dict) -> None: ...


class DbResearchStore:
    """Postgres + pgvector implementation. Uses its own short sessions so it never touches the turn's session."""

    def __init__(self, user_id: Any, session_factory: Callable | None = None):
        if session_factory is None:
            from app.db import SessionLocal

            session_factory = SessionLocal
        self.user_id = user_id
        self.sf = session_factory

    async def fresh_pages(self, canonicals: list[str], max_age_h: float = DEFAULT_MAX_AGE_H) -> dict[str, dict]:
        from app.models import ResearchPage

        if not canonicals or max_age_h <= 0:
            return {}
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_h)
        try:
            async with self.sf() as db:
                rows = (
                    await db.execute(
                        select(ResearchPage).where(
                            ResearchPage.user_id == self.user_id,
                            ResearchPage.canonical_url.in_(canonicals),
                            ResearchPage.fetched_at >= cutoff,
                        )
                    )
                ).scalars().all()
        except Exception as exc:  # noqa: BLE001 - memory is an optimization; fetch instead
            log.warning("research memory read failed: %s", type(exc).__name__)
            return {}
        return {
            r.canonical_url: {
                "ok": True,
                "url": r.url,
                "title": r.title,
                "text": r.text,
                "links": [tuple(x) for x in (r.links or []) if isinstance(x, (list, tuple)) and len(x) == 2],
                "fetched_at": r.fetched_at.isoformat() if r.fetched_at else "",
                "from_memory": True,
            }
            for r in rows
        }

    async def put_pages(self, rows: list[dict]) -> None:
        from sqlalchemy.dialects.postgresql import insert

        from app.models import ResearchPage

        if not rows:
            return
        now = datetime.now(timezone.utc)
        values = []
        for r in rows:
            vec = r.get("embedding")
            values.append(
                {
                    "user_id": self.user_id,
                    "canonical_url": str(r["canonical_url"])[:2000],
                    "url": str(r.get("url") or r["canonical_url"])[:2000],
                    "title": str(r.get("title") or "")[:300],
                    "text": str(r.get("text") or "")[:TEXT_MAX],
                    "note": str(r.get("note") or "")[:4000],
                    "links": [list(x) for x in (r.get("links") or [])][:200],
                    "goal": str(r.get("goal") or "")[:500],
                    "embedding": vec if _vec_ok(vec) else None,
                    "fetched_at": now,
                }
            )
        stmt = insert(ResearchPage).values(values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_research_pages_user_url",
            set_={c: stmt.excluded[c] for c in ("url", "title", "text", "note", "links", "goal", "embedding", "fetched_at")},
        )
        try:
            async with self.sf() as db:
                await db.execute(stmt)
                await db.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("research memory write failed: %s", type(exc).__name__)

    async def save_summary(self, row: dict) -> None:
        from app.models import ResearchSummary

        vec = row.get("embedding")
        try:
            async with self.sf() as db:
                db.add(
                    ResearchSummary(
                        user_id=self.user_id,
                        crawl_id=str(row.get("crawl_id") or "")[:40],
                        job_id=row.get("job_id"),
                        goal=str(row.get("goal") or "")[:500],
                        summary=str(row.get("summary") or "")[:8000],
                        sources=list(row.get("sources") or [])[:50],
                        embedding=vec if _vec_ok(vec) else None,
                    )
                )
                await db.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("research summary write failed: %s", type(exc).__name__)

    async def search(self, query: str, vec: list[float] | None = None, top: int = 10) -> dict:
        """Semantic search when an embedding is available, else keyword match; always this user's rows only."""
        from app.models import ResearchPage, ResearchSummary

        top = max(1, min(int(top or 10), 30))
        out: dict = {"pages": [], "summaries": []}
        try:
            async with self.sf() as db:
                if _vec_ok(vec):
                    lit = _vec_lit(vec)
                    for table, key, cols in (
                        ("research_pages", "pages", "url, title, note, fetched_at"),
                        ("research_summaries", "summaries", "crawl_id, goal, summary, sources, created_at"),
                    ):
                        rows = (
                            await db.execute(
                                text(
                                    f"SELECT {cols}, 1 - (embedding <=> CAST(:vec AS vector)) AS sim FROM {table} "  # noqa: S608 - fixed names
                                    "WHERE user_id = :uid AND embedding IS NOT NULL "
                                    "ORDER BY embedding <=> CAST(:vec AS vector) LIMIT :k"
                                ),
                                {"uid": self.user_id, "vec": lit, "k": top},
                            )
                        ).mappings().all()
                        out[key] = [_row(r) for r in rows]
                if not out["pages"] and not out["summaries"]:
                    words = [w for w in re.findall(r"[a-z0-9][a-z0-9\-]{2,}", (query or "").lower())][:6]
                    if words:
                        pcond = or_(*[ResearchPage.note.ilike(f"%{w}%") | ResearchPage.title.ilike(f"%{w}%") for w in words])
                        scond = or_(*[ResearchSummary.summary.ilike(f"%{w}%") | ResearchSummary.goal.ilike(f"%{w}%") for w in words])
                        pages = (
                            await db.execute(
                                select(ResearchPage)
                                .where(ResearchPage.user_id == self.user_id, pcond)
                                .order_by(ResearchPage.fetched_at.desc())
                                .limit(top)
                            )
                        ).scalars().all()
                        sums = (
                            await db.execute(
                                select(ResearchSummary)
                                .where(ResearchSummary.user_id == self.user_id, scond)
                                .order_by(ResearchSummary.created_at.desc())
                                .limit(top)
                            )
                        ).scalars().all()
                        out["pages"] = [
                            {"url": p.url, "title": p.title, "note": p.note, "fetched_at": p.fetched_at.isoformat()} for p in pages
                        ]
                        out["summaries"] = [
                            {"crawl_id": s.crawl_id, "goal": s.goal, "summary": s.summary, "sources": s.sources, "created_at": s.created_at.isoformat()}
                            for s in sums
                        ]
        except Exception as exc:  # noqa: BLE001
            log.warning("research memory search failed: %s", type(exc).__name__)
            out["error"] = "Research memory is unavailable right now."
        return out


def _row(r) -> dict:
    d = dict(r)
    for k in ("fetched_at", "created_at"):
        if d.get(k) is not None and hasattr(d[k], "isoformat"):
            d[k] = d[k].isoformat()
    if d.get("sim") is not None:
        d["sim"] = round(float(d["sim"]), 3)
    return d


class MemoryResearchStore:
    """In-process store with the same contract (tests, and when no DB is reachable)."""

    def __init__(self):
        self.pages: dict[str, dict] = {}
        self.summaries: list[dict] = []

    async def fresh_pages(self, canonicals, max_age_h=DEFAULT_MAX_AGE_H):
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_h)
        out = {}
        for c in canonicals:
            row = self.pages.get(c)
            if row and max_age_h > 0 and row["fetched_at"] >= cutoff:
                out[c] = {"ok": True, "url": row["url"], "title": row["title"], "text": row["text"], "links": row.get("links") or [], "from_memory": True}
        return out

    async def put_pages(self, rows):
        for r in rows:
            self.pages[r["canonical_url"]] = {**r, "fetched_at": r.get("fetched_at") or datetime.now(timezone.utc)}

    async def save_summary(self, row):
        self.summaries.append(dict(row))

    async def search(self, query, vec=None, top=10):
        words = set(re.findall(r"[a-z0-9][a-z0-9\-]{2,}", (query or "").lower()))
        pages = [p for p in self.pages.values() if words & set(re.findall(r"[a-z0-9\-]+", (p.get("note", "") + " " + p.get("title", "")).lower()))]
        sums = [s for s in self.summaries if words & set(re.findall(r"[a-z0-9\-]+", (s.get("summary", "") + " " + s.get("goal", "")).lower()))]
        return {"pages": pages[:top], "summaries": sums[:top]}
