from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agents.routes import router as agents_router
from app.automations.routes import router as automations_router, webhook_router
from app.auth.routes import me_router, router as auth_router
from app.chat.routes import router as chat_router
from app.config import get_settings
from app.connectors.routes import router as connectors_router
from app.connectors.deploy_routes import router as deploy_router
from app.connectors.github_status import router as github_router
from app.files import router as files_router
from app.db import init_db
from app.inbox.routes import router as inbox_router
from app.storage import ensure_bucket
from app.skills.routes import router as skills_router
from app.tasks.routes import router as tasks_router
from app.jobs.routes import router as jobs_router

log = logging.getLogger("assistant")
logging.basicConfig(level=logging.INFO)


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
    from app.inbox.worker import start_inbox_worker
    from app.jobs.worker import start_job_worker
    from app.tasks.scheduler import start_scheduler

    workers = [
        asyncio.create_task(_sync_builtins_later(), name="startup-sync"),
        await start_scheduler(),
        await start_job_worker(),
        await start_inbox_worker(),
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
    allow_origins=[s.public_web_url, "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
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
app.include_router(jobs_router, prefix="/api")
app.include_router(skills_router, prefix="/api")
app.include_router(inbox_router, prefix="/api")
app.include_router(files_router, prefix="/api")


@app.get("/health")
async def health():
    return {"ok": True}
