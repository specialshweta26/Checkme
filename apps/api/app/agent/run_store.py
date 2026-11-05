"""Durable checkpoints for background jobs, so a job restarted after a deploy or crash continues where it stopped.

A run's checkpoints are deleted when its turn ends. A failed read or write only costs the ability to resume, so it
never fails the job.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Sequence

from langchain_core.messages import ToolMessage
from langgraph.checkpoint.base import WRITES_IDX_MAP, BaseCheckpointSaver, CheckpointTuple, get_checkpoint_metadata
from sqlalchemy import cast, delete, select, update
from sqlalchemy.dialects.postgresql import JSONB

log = logging.getLogger(__name__)

KEEP_DAYS = 1  # rows left by a job that never came back; restarts happen within minutes, and these hold tool arguments
INTERRUPTED = (
    "This call was cut off by a restart, so it may or may not have taken effect. Check whether it did before "
    "repeating it, and don't repeat it if you can't tell and it sends, pays, deletes or changes something: "
    "say so to the user instead."
)


def _session_factory():
    from app.db import SessionLocal

    return SessionLocal


def job_thread(job: Any, query: str, confirmations: list | None, history: list | None) -> str:
    """One thread per job turn: a restart of the turn matches, while a reply or approval starts a new one."""
    seed = repr((query, confirmations or [], len(history or [])))
    return f"job:{job.id}:{hashlib.sha256(seed.encode()).hexdigest()[:16]}"


class DurableSaver(BaseCheckpointSaver[int]):
    """Stores a run's checkpoints in `run_checkpoints`. Async only, like the agent runs. Message history is kept as
    changes across checkpoints, so a run needs all of its checkpoints until it ends."""

    def __init__(self, session_factory: Any = None):
        super().__init__()
        self._factory = session_factory or _session_factory()

    def _tuple(self, row: Any) -> CheckpointTuple:
        checkpoint = self.serde.loads_typed((row.checkpoint_type, row.checkpoint))
        metadata = self.serde.loads_typed((row.checkpoint_type, row.meta))
        latest = {(w["task"], w["idx"]): w for w in row.writes or []}
        writes = [
            (w["task"], w["channel"], self.serde.loads_typed((w["type"], base64.b64decode(w["value"]))))
            for w in latest.values()
        ]
        base = {"thread_id": row.thread_id, "checkpoint_ns": row.ns}
        parent = {"configurable": {**base, "checkpoint_id": row.parent_id}} if row.parent_id else None
        return CheckpointTuple(
            config={"configurable": {**base, "checkpoint_id": row.checkpoint_id}},
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=parent,
            pending_writes=writes,
        )

    async def _rows(self, config: dict | None, before: dict | None, limit: int | None) -> list:
        from app.models import RunCheckpoint

        if not config:
            return []
        conf = config["configurable"]
        query = select(RunCheckpoint).where(RunCheckpoint.thread_id == conf["thread_id"])
        if conf.get("checkpoint_ns") is not None:
            query = query.where(RunCheckpoint.ns == conf["checkpoint_ns"])
        if conf.get("checkpoint_id"):
            query = query.where(RunCheckpoint.checkpoint_id == conf["checkpoint_id"])
        if before:
            query = query.where(RunCheckpoint.checkpoint_id < before["configurable"]["checkpoint_id"])
        query = query.order_by(RunCheckpoint.checkpoint_id.desc())
        if limit:
            query = query.limit(limit)
        try:
            async with self._factory() as db:
                return list((await db.execute(query)).scalars().all())
        except Exception as exc:  # noqa: BLE001 - see the module docstring
            log.warning("run checkpoint not read: %s", type(exc).__name__)
            return []

    async def aget_tuple(self, config: dict) -> CheckpointTuple | None:
        conf = config["configurable"]
        rows = await self._rows({"configurable": {**conf, "checkpoint_ns": conf.get("checkpoint_ns", "")}}, None, 1)
        return await asyncio.to_thread(self._tuple, rows[0]) if rows else None

    async def alist(self, config: dict | None, *, filter=None, before=None, limit=None) -> AsyncIterator[CheckpointTuple]:
        for row in await self._rows(config, before, limit):
            yield await asyncio.to_thread(self._tuple, row)

    async def aput(self, config: dict, checkpoint: dict, metadata: dict, new_versions: dict) -> dict:
        from app.models import RunCheckpoint

        conf = config["configurable"]
        thread, ns = conf["thread_id"], conf.get("checkpoint_ns", "")
        saved = {"configurable": {"thread_id": thread, "checkpoint_ns": ns, "checkpoint_id": checkpoint["id"]}}
        try:

            def dump() -> tuple[str, bytes, bytes]:
                kind, blob = self.serde.dumps_typed(checkpoint)
                return kind, blob, self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))[1]

            kind, blob, meta = await asyncio.to_thread(dump)
            async with self._factory() as db:
                db.add(
                    RunCheckpoint(
                        thread_id=thread,
                        ns=ns,
                        checkpoint_id=checkpoint["id"],
                        parent_id=conf.get("checkpoint_id"),
                        checkpoint_type=kind,
                        checkpoint=blob,
                        meta=meta,
                        writes=[],
                    )
                )
                await db.commit()
        except Exception as exc:  # noqa: BLE001 - see the module docstring
            log.warning("run checkpoint not saved: %s", type(exc).__name__)
        return saved

    async def aput_writes(self, config: dict, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = "") -> None:
        from app.models import RunCheckpoint

        conf = config["configurable"]
        try:

            def dump() -> list[dict]:
                rows = []
                for idx, (channel, value) in enumerate(writes):
                    kind, blob = self.serde.dumps_typed(value)
                    rows.append({"task": task_id, "idx": WRITES_IDX_MAP.get(channel, idx), "channel": channel, "type": kind, "value": base64.b64encode(blob).decode()})
                return rows

            rows = await asyncio.to_thread(dump)
            async with self._factory() as db:
                await db.execute(
                    update(RunCheckpoint)
                    .where(
                        RunCheckpoint.thread_id == conf["thread_id"],
                        RunCheckpoint.ns == conf.get("checkpoint_ns", ""),
                        RunCheckpoint.checkpoint_id == conf.get("checkpoint_id"),
                    )
                    .values(writes=RunCheckpoint.writes.op("||")(cast(rows, JSONB)))
                )
                await db.commit()
        except Exception as exc:  # noqa: BLE001 - see the module docstring
            log.warning("run checkpoint writes not saved: %s", type(exc).__name__)

    async def adelete_thread(self, thread_id: str) -> None:
        from app.models import RunCheckpoint

        try:
            async with self._factory() as db:
                await db.execute(delete(RunCheckpoint).where(RunCheckpoint.thread_id == thread_id))
                await db.commit()
        except Exception as exc:  # noqa: BLE001 - the age limit clears it later
            log.info("run checkpoint not deleted: %s", type(exc).__name__)


async def prune_old(session_factory: Any) -> None:
    from app.models import RunCheckpoint

    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    async with session_factory() as db:
        await db.execute(delete(RunCheckpoint).where(RunCheckpoint.updated_at < cutoff))
        await db.commit()


async def settle_interrupted_calls(graph: Any, config: dict) -> int:
    """Answer tool calls cut off by the restart, so they aren't run a second time. Finished ones keep their results."""
    state = await graph.aget_state(config)
    messages = (state.values or {}).get("messages") or []
    last = messages[-1] if messages else None
    calls = getattr(last, "tool_calls", None) or []
    if not calls:
        return 0
    finished: dict[str, Any] = {}
    for task in state.tasks or ():
        result = getattr(task, "result", None)
        for msg in (result or {}).get("messages", []) if isinstance(result, dict) else []:
            if getattr(msg, "tool_call_id", None):
                finished[msg.tool_call_id] = msg
    replies = [finished.get(c["id"]) or ToolMessage(content=INTERRUPTED, tool_call_id=c["id"], name=c.get("name")) for c in calls]
    await graph.aupdate_state(config, {"messages": replies}, as_node="tools")
    return sum(c["id"] not in finished for c in calls)
