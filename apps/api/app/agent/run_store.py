"""Durable checkpoints for background jobs, so a job restarted after a deploy or crash continues where it stopped.

A job keeps one thread across its turns, so after an approval or a reply it continues with everything it did and saw,
not only the text of earlier replies. Checkpoints are deleted when the job ends. A failed read or write only costs the
ability to resume, so it never fails the job.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Sequence

from langchain_core.messages import ToolMessage
from langgraph.checkpoint.base import WRITES_IDX_MAP, BaseCheckpointSaver, CheckpointTuple, get_checkpoint_metadata
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

log = logging.getLogger(__name__)

KEEP_DAYS = 1  # for jobs idle this long: they hold tool arguments, and a job without them falls back to reply text
INTERRUPTED = (
    "This call was cut off by a restart, so it may or may not have taken effect. Check whether it did before "
    "repeating it, and don't repeat it if you can't tell and it sends, pays, deletes or changes something: "
    "say so to the user instead."
)


def _session_factory():
    from app.db import SessionLocal

    return SessionLocal


def job_thread(job: Any) -> str:
    return f"job:{job.id}"


async def job_turn_input(graph: Any, config: dict, job: Any, payload: dict, new_messages: list) -> tuple[dict | None, str]:
    """What to send a job's graph this turn, and how the turn relates to earlier ones.

    "restart": continue the interrupted turn. "continue": add this turn's messages to the kept run. "new": no kept run,
    so send the full payload, which rebuilds context from the text history.
    """
    try:
        state = await graph.aget_state(config)
    except Exception as exc:  # noqa: BLE001 - see the module docstring
        log.warning("run checkpoint not read: %s", type(exc).__name__)
        return payload, "new"
    if not (state.values or {}).get("messages"):
        return payload, "new"
    await settle_interrupted_calls(graph, config)
    if (job.meta or {}).get("restarted"):
        return None, "restart"
    return {"messages": new_messages}, "continue"


def confirmed_key(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


async def claim_confirmed(ctx: Any, payload: dict) -> bool:
    """Whether to run this approved action. A job records it first, so a restart can't run it a second time."""
    job = getattr(ctx, "job", None)
    if job is None or getattr(ctx, "db", None) is None:
        return True
    meta = dict(job.meta or {})
    started = list(meta.get("confirmed_started") or [])
    key = confirmed_key(payload)
    if key in started:
        return False
    meta["confirmed_started"] = [*started, key]
    job.meta = meta
    await ctx.db.commit()
    return True


class DurableSaver(BaseCheckpointSaver[int]):
    """Stores a run's checkpoints in `run_checkpoints`. Async only, like the agent runs. Message history is kept as
    changes across checkpoints, so a run needs all of its checkpoints until it ends."""

    def __init__(self, session_factory: Any = None):
        super().__init__()
        self._factory = session_factory or _session_factory()

    def _tuple(self, row: Any, write_rows: list) -> CheckpointTuple:
        checkpoint = self.serde.loads_typed((row.checkpoint_type, row.checkpoint))
        metadata = self.serde.loads_typed((row.checkpoint_type, row.meta))
        writes = [(w.task, w.channel, self.serde.loads_typed((w.value_type, w.value))) for w in write_rows]
        base = {"thread_id": row.thread_id, "checkpoint_ns": row.ns}
        parent = {"configurable": {**base, "checkpoint_id": row.parent_id}} if row.parent_id else None
        return CheckpointTuple(
            config={"configurable": {**base, "checkpoint_id": row.checkpoint_id}},
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=parent,
            pending_writes=writes,
        )

    async def _rows(self, config: dict | None, before: dict | None, limit: int | None) -> list[tuple]:
        """(checkpoint row, its write rows), newest first."""
        from app.models import RunCheckpoint, RunCheckpointWrite

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
                rows = list((await db.execute(query)).scalars().all())
                if not rows:
                    return []
                writes = (
                    await db.execute(
                        select(RunCheckpointWrite)
                        .where(
                            RunCheckpointWrite.thread_id == conf["thread_id"],
                            RunCheckpointWrite.checkpoint_id.in_([r.checkpoint_id for r in rows]),
                        )
                        .order_by(RunCheckpointWrite.id)
                    )
                ).scalars().all()
                return [
                    (r, [w for w in writes if w.checkpoint_id == r.checkpoint_id and w.ns == r.ns]) for r in rows
                ]
        except Exception as exc:  # noqa: BLE001 - see the module docstring
            log.warning("run checkpoint not read: %s", type(exc).__name__)
            return []

    async def aget_tuple(self, config: dict) -> CheckpointTuple | None:
        conf = config["configurable"]
        rows = await self._rows({"configurable": {**conf, "checkpoint_ns": conf.get("checkpoint_ns", "")}}, None, 1)
        return await asyncio.to_thread(self._tuple, *rows[0]) if rows else None

    async def alist(self, config: dict | None, *, filter=None, before=None, limit=None) -> AsyncIterator[CheckpointTuple]:
        for row, writes in await self._rows(config, before, limit):
            yield await asyncio.to_thread(self._tuple, row, writes)

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
                    )
                )
                await db.commit()
        except Exception as exc:  # noqa: BLE001 - see the module docstring
            log.warning("run checkpoint not saved: %s", type(exc).__name__)
        return saved

    async def aput_writes(self, config: dict, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = "") -> None:
        from app.models import RunCheckpointWrite

        conf = config["configurable"]
        base = {"thread_id": conf["thread_id"], "ns": conf.get("checkpoint_ns", ""), "checkpoint_id": conf.get("checkpoint_id"), "task": task_id}
        try:

            def dump() -> list[dict]:
                rows = []
                for idx, (channel, value) in enumerate(writes):
                    kind, blob = self.serde.dumps_typed(value)
                    rows.append({**base, "idx": WRITES_IDX_MAP.get(channel, idx), "channel": channel, "value_type": kind, "value": blob})
                return rows

            rows = await asyncio.to_thread(dump)
            if not rows:
                return
            stmt = insert(RunCheckpointWrite).values(rows)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_run_checkpoint_writes",
                set_={"channel": stmt.excluded.channel, "value_type": stmt.excluded.value_type, "value": stmt.excluded.value},
            )
            async with self._factory() as db:
                await db.execute(stmt)
                await db.commit()
        except Exception as exc:  # noqa: BLE001 - see the module docstring
            log.warning("run checkpoint writes not saved: %s", type(exc).__name__)

    async def adelete_thread(self, thread_id: str) -> None:
        from app.models import RunCheckpoint, RunCheckpointWrite

        try:
            async with self._factory() as db:
                await db.execute(delete(RunCheckpoint).where(RunCheckpoint.thread_id == thread_id))
                await db.execute(delete(RunCheckpointWrite).where(RunCheckpointWrite.thread_id == thread_id))
                await db.commit()
        except Exception as exc:  # noqa: BLE001 - the age limit clears it later
            log.info("run checkpoint not deleted: %s", type(exc).__name__)


async def prune_old(session_factory: Any) -> None:
    from app.models import RunCheckpoint, RunCheckpointWrite

    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    # Whole threads only: message history spans a thread's checkpoints, so a partial one can't be read correctly.
    idle = select(RunCheckpoint.thread_id).group_by(RunCheckpoint.thread_id).having(func.max(RunCheckpoint.updated_at) < cutoff)
    async with session_factory() as db:
        ids = list((await db.execute(idle)).scalars().all())
        if ids:
            await db.execute(delete(RunCheckpoint).where(RunCheckpoint.thread_id.in_(ids)))
            await db.execute(delete(RunCheckpointWrite).where(RunCheckpointWrite.thread_id.in_(ids)))
            await db.commit()


async def forget_job(job: Any) -> None:
    """The job is over, so nothing is left to resume."""
    await DurableSaver().adelete_thread(job_thread(job))


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
