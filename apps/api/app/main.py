from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.agents.routes import router as agents_router
from app.automations.routes import router as automations_router, webhook_router
from app.auth.routes import me_router, router as auth_router
from app.channels.routes import router as channels_router
from app.chat.routes import router as chat_router
from app.config import get_settings
from app.connectors.routes import router as connectors_router
from app.connectors.deploy_routes import router as deploy_router
from app.connectors.github_status import router as github_router
from app.desktop.routes import router as desktop_router
from app.files import router as files_router
from app.db import init_db
from app.inbox.routes import router as inbox_router
from app.storage import ensure_bucket
from app.skills.routes import router as skills_router
from app.browser_runs.routes import login_router as browser_logins_router
from app.browser_runs.routes import hook_router as browser_hook_router
from app.browser_runs.routes import runs_router as browser_history_router
from app.browser_runs.routes import router as browser_runs_router
from app.tasks.routes import router as tasks_router
from app.jobs.routes import router as jobs_router

log = logging.getLogger("assistant")
logging.basicConfig(level=logging.INFO)


async def _reclaim_redis_later() -> None:
    from app.cache import reclaim_if_full

    for delay in (0, 5, 15, 30, 45, 60):
        if delay:
            await asyncio.sleep(delay)
        try:
            out = await asyncio.wait_for(reclaim_if_full(), 15)
        except Exception as vis_exc:  # noqa: BLE001
            log.warning("redis reclaim skipped: %s", vis_exc)
            continue
        if out.get("ok"):
            return


async def _sync_builtins_later() -> None:
    await asyncio.sleep(0.2)
    try:
        from sqlalchemy import select

        from app.connectors.service import sync_builtin
        from app.db import SessionLocal
        from app.models import Connector

        async with SessionLocal() as db:
            rows = (await db.execute(select(Connector).where(Connector.kind == "builtin"))).scalars().all()
            for row in rows:
                try:
                    await sync_builtin(db, row)
                except Exception as vis_exc:  # noqa: BLE001
                    log.warning("builtin sync skipped for %s: %s", row.slug, vis_exc)
                await asyncio.sleep(0)
    except Exception as vis_exc:  # noqa: BLE001
        log.warning("startup connector sync skipped: %s", vis_exc)
    await _resync_reset_mcp()


async def _resync_reset_mcp() -> None:
    """Re-read user MCP servers whose tools the policy migration reset to needs-approval (ToolIndex.policy_v 2), so
    tools the server marks read-only stop asking. Each connector is re-synced once; a failure leaves it asking."""
    try:
        from sqlalchemy import select

        from app.connectors.service import sync_connector
        from app.db import SessionLocal
        from app.models import Connector, ToolIndex

        async with SessionLocal() as db:
            keys = set((await db.execute(select(ToolIndex.server_key).where(ToolIndex.policy_v == 2).distinct())).scalars().all())
            if not keys:
                return
            rows = (
                await db.execute(select(Connector).where(Connector.kind != "builtin", Connector.server_key.in_(keys)))
            ).scalars().all()
            for row in rows:
                try:
                    await sync_connector(db, row)
                except Exception as vis_exc:  # noqa: BLE001
                    log.warning("mcp re-sync after policy reset skipped for %s: %s", row.slug, vis_exc)
                await asyncio.sleep(0)
    except Exception as vis_exc:  # noqa: BLE001
        log.warning("mcp policy re-sync skipped: %s", vis_exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_db()
    _s = get_settings()
    log.info(
        "computer backend: %s (COMPUTER_PROVIDER=%s, E2B key set=%s, Daytona key set=%s)",
        _s.computer_backend or "none",
        _s.computer_provider or "unset",
        bool(_s.e2b_api_key),
        bool(_s.daytona_api_key),
    )
    try:
        ensure_bucket()
    except Exception as vis_exc:  # noqa: BLE001
        log.warning("object storage not ready: %s", vis_exc)
    from app.agent.memory_worker import start_memory_worker
    from app.memory_engine.learn import start_memory_learner
    from app.memory_engine.consolidate import start_memory_consolidator
    from app.loop_watch import watch_loop
    from app.inbox.worker import start_inbox_worker
    from app.inbox.push import start_mail_push
    from app.automations.triggers import start_meeting_triggers
    from app.jobs.worker import start_job_worker
    from app.tasks.scheduler import start_scheduler

    workers = [
        asyncio.create_task(_reclaim_redis_later(), name="redis-reclaim"),
        asyncio.create_task(_sync_builtins_later(), name="startup-sync"),
        asyncio.create_task(watch_loop(), name="loop-watch"),
        await start_scheduler(),
        await start_job_worker(),
        await start_inbox_worker(),
        await start_mail_push(),
        await start_meeting_triggers(),
        await start_memory_worker(),
        await start_memory_learner(),
        await start_memory_consolidator(),
    ]
    cfg = get_settings()
    log.info(
        "oauth google=%s microsoft=%s public_web_url=%s db=%s",
        bool(cfg.google_client_id),
        bool(cfg.microsoft_client_id),
        cfg.public_web_url,
        cfg.database_host,
    )
    try:
        yield
    finally:
        for worker in workers:
            worker.cancel()
        for worker in workers:
            try:
                await worker
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Lumio", lifespan=lifespan)
s = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[s.public_web_url, "http://localhost:3000", "tauri://localhost", "https://tauri.localhost"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def lumio_client_kind(request: Request, call_next):
    from app.desktop.client import reset_client_kind, set_client_kind

    token = set_client_kind(request.headers.get("x-lumio-client") or "")
    try:
        return await call_next(request)
    finally:
        reset_client_kind(token)


app.include_router(auth_router, prefix="/api")
app.include_router(me_router, prefix="/api")
app.include_router(chat_router, prefix="/api")
app.include_router(agents_router, prefix="/api")
app.include_router(github_router, prefix="/api")
app.include_router(deploy_router, prefix="/api")
app.include_router(connectors_router, prefix="/api")
app.include_router(tasks_router, prefix="/api")
app.include_router(automations_router, prefix="/api")
app.include_router(webhook_router, prefix="/api")
app.include_router(channels_router, prefix="/api")
app.include_router(jobs_router, prefix="/api")
app.include_router(skills_router, prefix="/api")
app.include_router(browser_runs_router, prefix="/api")
app.include_router(browser_logins_router, prefix="/api")
app.include_router(browser_history_router, prefix="/api")
app.include_router(browser_hook_router, prefix="/api")
app.include_router(inbox_router, prefix="/api")
app.include_router(files_router, prefix="/api")
app.include_router(desktop_router, prefix="/api")


@app.get("/health")
async def health():
    return {"ok": True}
