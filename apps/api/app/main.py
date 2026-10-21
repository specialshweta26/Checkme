from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.auth.routes import me_router, router as auth_router
from app.chat.routes import router as chat_router
from app.config import get_settings
from app.connectors.routes import router as connectors_router
from app.files import router as files_router
from app.db import init_db
from app.inbox.routes import router as inbox_router
from app.storage import ensure_bucket
from app.tasks.routes import router as tasks_router

log = logging.getLogger("assistant")
logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_db()
    try:
        ensure_bucket()
    except Exception as exc:  # noqa: BLE001
        log.warning("object storage not ready: %s", exc)
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
                except Exception as exc:  # noqa: BLE001
                    log.warning("builtin sync skipped for %s: %s", row.slug, exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("startup connector sync skipped: %s", exc)
    from app.inbox.worker import start_inbox_worker
    from app.tasks.scheduler import start_scheduler

    workers = [await start_scheduler(), await start_inbox_worker()]
    cfg = get_settings()
    log.info(
        "oauth google=%s microsoft=%s public_web_url=%s",
        bool(cfg.google_client_id),
        bool(cfg.microsoft_client_id),
        cfg.public_web_url,
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


app = FastAPI(title="Personal Assistant", lifespan=lifespan)
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
app.include_router(connectors_router, prefix="/api")
app.include_router(tasks_router, prefix="/api")
app.include_router(inbox_router, prefix="/api")
app.include_router(files_router, prefix="/api")


@app.get("/health")
async def health():
    return {"ok": True}
