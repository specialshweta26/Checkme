"""Run history (docs/BROWSING.md "Run history"): every browser_task run is kept step by step, with what was done, the
planner's one-line reason, whether it worked, the page and a small screenshot, so the user can watch a run afterwards
and see why it stopped. Best effort: nothing here may break or slow a run noticeably.

Healing and AI steps run browser_task inside a run; they add their steps to that run (``CURRENT``) instead of
starting their own.
"""

from __future__ import annotations

import asyncio
import io
import logging
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

MAX_STEPS = 80
MAX_SHOTS = 60
KEEP_RUNS = 100
SHOT_WIDTH = 720
SHOT_WAIT_S = 3.0
CURRENT: ContextVar["RunLog | None"] = ContextVar("browser_run_log", default=None)
FINAL = {"completed", "replayed", "recovered", "stopped", "needs_you"}


def thumbnail(png: bytes, width: int = SHOT_WIDTH) -> bytes:
    """A small JPEG of a screen frame, or b"" if it is not an image."""
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(png)).convert("RGB")
        if img.width > width:
            img = img.resize((width, max(1, round(img.height * width / img.width))))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=60, optimize=True)
        return out.getvalue()
    except Exception:  # noqa: BLE001
        return b""


async def latest_frame(user_id) -> bytes:
    """The newest screen frame for this user: the cloud computer refreshes it after each command (wait briefly for
    one in flight), the desktop app sends one with each snapshot."""
    from app.connectors import daytona_box as box

    task = box._SHOT_TASKS.get(str(user_id))
    if task is not None and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), SHOT_WAIT_S)
        except Exception:  # noqa: BLE001
            pass
    return box.live_screen_png(str(user_id))


def status_for(out: dict) -> str:
    """The run's outcome from browser_task's result."""
    if out.get("healed"):
        return "recovered" if out.get("ok") else "stopped"
    if out.get("need_user") or out.get("stopped_before"):
        return "needs_you"
    if out.get("ok") and out.get("done"):
        return "replayed" if out.get("via") == "replay" else "completed"
    return "stopped"


class RunLog:
    def __init__(self, user_id, goal: str, host: str = "") -> None:
        self.user_id, self.goal, self.host = user_id, goal, host
        self.id = uuid.uuid4()
        self.steps: list[dict] = []
        self.shots = 0
        self.trace_id = ""
        self._saved = False
        self._pending: list[asyncio.Task] = []

    async def step(self, action: str, why: str = "", ok: bool = True, state: dict | None = None, error: str = "",
                   shot: bool = True) -> None:  # fmt: skip
        if len(self.steps) >= MAX_STEPS:
            return
        st = state or {}
        row: dict[str, Any] = {
            "i": len(self.steps) + 1, "at": datetime.now(timezone.utc).isoformat(), "action": action[:300], "why": why[:400],
            "ok": bool(ok), "url": str(st.get("url") or "")[:500], "title": str(st.get("title") or "")[:200],
        }  # fmt: skip
        if error:
            row["error"] = error[:300]
        warnings = [str(w)[:200] for w in (st.get("warnings") or [])][:2]
        if warnings:
            row["warnings"] = warnings
        self.steps.append(row)
        if shot and self.shots < MAX_SHOTS:
            self.shots += 1
            self._pending.append(asyncio.create_task(self._shot(row)))

    async def _shot(self, row: dict) -> None:
        try:
            png = await latest_frame(self.user_id)
            jpg = await asyncio.to_thread(thumbnail, png) if png else b""
            if not jpg:
                return
            from app.storage import put_bytes

            key = f"{self.user_id}/browser-runs/{self.id}/{row['i']}.jpg"
            await asyncio.to_thread(put_bytes, key, jpg, "image/jpeg")
            row["shot"] = key
        except Exception as exc:  # noqa: BLE001
            log.info("run screenshot skipped: %s", type(exc).__name__)

    async def finish(self, out: dict, trace_id: str = "") -> None:
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
        self.trace_id = str(trace_id or out.get("saved_trace_id") or "")
        status = status_for(out)
        result = str(out.get("result") or out.get("need_user") or out.get("note") or out.get("error") or "")[:2000]
        if out.get("heal_note"):
            result = (result + " " + str(out["heal_note"])).strip()[:2000]
        try:
            from app.db import SessionLocal
            from app.models import BrowserRun

            async with SessionLocal() as db:
                db.add(BrowserRun(id=self.id, user_id=uuid.UUID(str(self.user_id)), trace_id=self.trace_id, goal=self.goal[:4000],
                                  host=self.host[:255], status=status, result=result, steps=self.steps,
                                  finished_at=datetime.now(timezone.utc)))  # fmt: skip
                await db.commit()
            self._saved = True
            await prune(self.user_id)
        except Exception as exc:  # noqa: BLE001
            log.info("browser run log skipped: %s", type(exc).__name__)


async def prune(user_id, keep: int = KEEP_RUNS) -> int:
    """Keep the newest ``keep`` runs per user; delete older rows and their screenshots."""
    from sqlalchemy import delete, select

    from app.db import SessionLocal
    from app.models import BrowserRun

    uid = uuid.UUID(str(user_id))
    async with SessionLocal() as db:
        q = select(BrowserRun.id, BrowserRun.steps).where(BrowserRun.user_id == uid).order_by(BrowserRun.started_at.desc()).offset(keep)
        old = (await db.execute(q)).all()
        if not old:
            return 0
        keys = [s.get("shot") for _, steps in old for s in (steps or []) if isinstance(s, dict) and s.get("shot")]
        await db.execute(delete(BrowserRun).where(BrowserRun.id.in_([rid for rid, _ in old])))
        await db.commit()
    if keys:
        from app.storage import delete_keys

        try:
            await asyncio.to_thread(delete_keys, keys)
        except Exception as exc:  # noqa: BLE001
            log.info("old run screenshots not deleted: %s", type(exc).__name__)
    return len(old)


def _shot_url(key: str) -> str:
    from app.storage import file_url

    return file_url(key, key.rsplit("/", 1)[-1], "image/jpeg", inline=True)


def describe(row, with_steps: bool = False) -> dict:
    steps = list(row.steps or [])
    out = {
        "id": str(row.id), "goal": row.goal, "host": row.host, "status": row.status, "result": row.result,
        "trace_id": row.trace_id or None, "steps": len(steps), "failed_steps": sum(1 for s in steps if not s.get("ok", True)),
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }  # fmt: skip
    if with_steps:
        out["step_rows"] = [{**{k: v for k, v in s.items() if k != "shot"}, "shot_url": _shot_url(s["shot"]) if s.get("shot") else None} for s in steps]
    return out


async def list_runs(user_id, trace_id: str = "", limit: int = 50) -> list[dict]:
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import BrowserRun

    q = select(BrowserRun).where(BrowserRun.user_id == uuid.UUID(str(user_id)))
    if trace_id:
        q = q.where(BrowserRun.trace_id == str(trace_id))
    async with SessionLocal() as db:
        rows = (await db.execute(q.order_by(BrowserRun.started_at.desc()).limit(max(1, min(limit, 100))))).scalars().all()
    return [describe(r) for r in rows]


async def get_run(user_id, run_id: str) -> dict | None:
    from app.db import SessionLocal
    from app.models import BrowserRun

    try:
        rid = uuid.UUID(str(run_id))
    except ValueError:
        return None
    async with SessionLocal() as db:
        row = await db.get(BrowserRun, rid)
    if row is None or str(row.user_id) != str(user_id):
        return None
    return describe(row, with_steps=True)


SHOTS_KEEP_DAYS = 30


async def tidy_screenshots(session_factory, user_id, days: int = SHOTS_KEEP_DAYS) -> dict:
    """Delete screenshots of this user's runs older than ``days`` (the steps, reasons and result stay; the run just has
    no pictures), except the newest run of each saved run, and screenshot files whose run no longer exists."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func, select

    from app.models import BrowserRun, BrowserTrace
    from app.storage import delete_keys, list_keys

    uid = uuid.UUID(str(user_id))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    async with session_factory() as db:
        saved = select(BrowserTrace.id.cast(BrowserRun.trace_id.type)).where(BrowserTrace.user_id == uid)
        newest_per_saved = (
            select(BrowserRun.trace_id, func.max(BrowserRun.started_at).label("at"))
            .where(BrowserRun.user_id == uid, BrowserRun.trace_id.in_(saved)).group_by(BrowserRun.trace_id).subquery()
        )
        keep_ids = set((await db.execute(
            select(BrowserRun.id).join(newest_per_saved, (newest_per_saved.c.trace_id == BrowserRun.trace_id)
                                       & (newest_per_saved.c.at == BrowserRun.started_at))
        )).scalars().all())
        old = (await db.execute(select(BrowserRun).where(BrowserRun.user_id == uid, BrowserRun.started_at < cutoff))).scalars().all()
        keys: list[str] = []
        for run in old:
            if run.id in keep_ids:
                continue
            steps = [dict(s) for s in (run.steps or []) if isinstance(s, dict)]
            shots = [s.pop("shot") for s in steps if s.get("shot")]
            if shots:
                keys += shots
                run.steps = steps
        live = {str(r) for r in (await db.execute(select(BrowserRun.id).where(BrowserRun.user_id == uid))).scalars()}
        await db.commit()
    try:
        stored = await asyncio.to_thread(list_keys, f"{uid}/browser-runs/")
    except Exception as exc:  # noqa: BLE001
        log.info("screenshot listing skipped: %s", type(exc).__name__)
        stored = []
    orphans = [k for k in stored if k.split("/")[2] not in live]
    if keys or orphans:
        try:
            await asyncio.to_thread(delete_keys, keys + orphans)
        except Exception as exc:  # noqa: BLE001
            log.info("old screenshots not deleted: %s", type(exc).__name__)
            return {"old": 0, "orphans": 0}
    return {"old": len(keys), "orphans": len(orphans)}
