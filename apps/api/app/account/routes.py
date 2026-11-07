"""Your data: download everything the app holds about you, or delete your account and all of it.

Export (GET /api/me/export): a ZIP with one JSON file per kind of data (every table row with your user_id, minus
encrypted credentials and secret hashes) and your stored files. Delete (POST /api/me/delete, with your email typed as
confirmation): your cloud computers, stored files, assistant memory service copies, then every database row with your
user_id and your user record, in one transaction; then you are signed out. Only the signed-in user's own data is
touched.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import zipfile
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import current_user
from app.models import Base, User

log = logging.getLogger(__name__)
router = APIRouter(tags=["account"])

EXPORT_FILES_MAX_BYTES = 200 * 1024 * 1024  # beyond this, files are listed rather than included


def _user_tables():
    """Every table with a user_id column, children before parents (safe delete order)."""
    return [t for t in reversed(Base.metadata.sorted_tables) if "user_id" in t.c and t.name != "users"]


def _secret(column: str) -> bool:
    return column.endswith("_enc") or column.endswith("_hash")


def _skipped(column) -> bool:
    """Credentials, and internal columns that are large and mean nothing to a person (embeddings, search index)."""
    kind = type(column.type).__name__.lower()
    return _secret(column.name) or column.name in ("embedding", "search_tsv") or "vector" in kind or "tsvector" in kind


def _plain(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return None  # binary blobs (embeddings, images) are not useful in an export
    if hasattr(value, "tolist"):
        return None  # pgvector embeddings
    return value if isinstance(value, (str, int, float, bool, list, dict, type(None))) else str(value)


async def export_rows(db: AsyncSession, user_id) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for table in _user_tables():
        cols = [c for c in table.c if not _skipped(c)]
        rows = (await db.execute(select(*cols).where(table.c.user_id == user_id))).mappings().all()
        if rows:
            out[table.name] = [{k: _plain(v) for k, v in row.items()} for row in rows]
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
    out["user"] = [{"id": str(user.id), "email": user.email, "display_name": user.display_name, "timezone": user.timezone,
                    "created_at": _plain(user.created_at)}]
    return out


def _zip(rows: dict[str, list[dict]], user_id):
    """The export as a temporary file (on disk past a few MB), files added one at a time, so a large account never
    has to fit in the server's memory."""
    import tempfile

    from app import storage

    buf = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, items in rows.items():
            zf.writestr(f"data/{name}.json", json.dumps(items, ensure_ascii=False, indent=1, default=str))
        try:
            keys = storage.list_keys(f"{user_id}/")
        except Exception:  # noqa: BLE001
            log.warning("export: listing files failed", exc_info=True)
            keys = []
        total, skipped = 0, []
        for key in keys:
            try:
                data, _ctype = storage.get_bytes(key)
            except Exception:  # noqa: BLE001
                skipped.append(key)
                continue
            if total + len(data) > EXPORT_FILES_MAX_BYTES:
                skipped.append(key)
                continue
            total += len(data)
            zf.writestr(f"files/{key.split('/', 1)[1]}", data)
            del data
        log.info("export for %s: %s tables, %s files (%.1f MB), %s skipped", user_id, len(rows), len(keys) - len(skipped),
                 total / 1e6, len(skipped))
        zf.writestr("README.txt", (
            "Your Lumio data export.\n\ndata/: one JSON file per kind of data (chats, memory, inbox cards, tasks, usage...).\n"
            "Encrypted sign-in credentials are not included.\nfiles/: files you uploaded or the assistant saved for you.\n"
            + (f"\nNot included (too large or unreadable): {len(skipped)} files.\n" if skipped else "")
        ))
    buf.seek(0)
    return buf


@router.get("/me/export")
async def export_my_data(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = await export_rows(db, user.id)
    buf = await asyncio.to_thread(_zip, rows, user.id)
    del rows
    name = f"lumio-export-{date.today().isoformat()}.zip"

    def chunks():
        try:
            while True:
                part = buf.read(1024 * 1024)
                if not part:
                    return
                yield part
        finally:
            buf.close()

    return StreamingResponse(chunks(), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


class DeleteIn(BaseModel):
    confirm: str  # the account's email, typed by the user


async def _outside_copies(db: AsyncSession, user: User) -> dict:
    """Best effort, before the rows go: cloud computers, stored files, the memory service's copies."""
    from app import storage
    from app.connectors import e2b_box
    from app.models import WorkspaceAgent

    done = {"computers": 0, "files": 0, "memory_agents": 0}
    try:
        done["computers"] = await asyncio.to_thread(e2b_box.kill_user_sandboxes, user.id)
    except Exception:  # noqa: BLE001
        log.warning("delete account: computers", exc_info=True)
    try:
        keys = await asyncio.to_thread(storage.list_keys, f"{user.id}/")
        await asyncio.to_thread(storage.delete_keys, keys)
        done["files"] = len(keys)
    except Exception:  # noqa: BLE001
        log.warning("delete account: files", exc_info=True)
    try:
        from app.agent.memory import forget_letta_agent

        for agent in (await db.execute(select(WorkspaceAgent).where(WorkspaceAgent.user_id == user.id))).scalars():
            await forget_letta_agent(agent, user)
            done["memory_agents"] += 1
    except Exception:  # noqa: BLE001
        log.warning("delete account: memory service", exc_info=True)
    return done


async def delete_user_data(db: AsyncSession, user_id) -> int:
    """Every row with this user_id, their chat run checkpoints, and the user. One transaction."""
    from app.models import AgentJob, ChatSession

    sessions = [str(s) for s in (await db.execute(select(ChatSession.id).where(ChatSession.user_id == user_id))).scalars()]
    jobs = [f"job:{j}" for j in (await db.execute(select(AgentJob.id).where(AgentJob.user_id == user_id))).scalars()]
    removed = 0
    for prefix in sessions:  # chat threads are "<session id>" or "<session id><peer suffix>"
        for table in ("run_checkpoint_writes", "run_checkpoints"):
            removed += (await db.execute(text(f"DELETE FROM {table} WHERE thread_id LIKE :p"), {"p": f"{prefix}%"})).rowcount or 0
    for thread in jobs:
        for table in ("run_checkpoint_writes", "run_checkpoints"):
            removed += (await db.execute(text(f"DELETE FROM {table} WHERE thread_id = :t"), {"t": thread})).rowcount or 0
    for table in _user_tables():
        removed += (await db.execute(delete(table).where(table.c.user_id == user_id))).rowcount or 0
    removed += (await db.execute(delete(User).where(User.id == user_id))).rowcount or 0
    await db.commit()
    return removed


@router.post("/me/delete")
async def delete_my_account(body: DeleteIn, response: Response, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if " ".join(body.confirm.split()).lower() != (user.email or "").lower():
        raise HTTPException(400, "Type your account's email address to confirm.")
    from app.security import SESSION_COOKIE

    outside = await _outside_copies(db, user)
    rows = await delete_user_data(db, user.id)
    log.info("account deleted: %s rows, %s", rows, outside)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True, "deleted_rows": rows, **outside}
