"""Run traces (docs/RUN_TRACES.md): every background job is kept step by step so the user can see why it did what it
did. Each tool call becomes one row: the tool, a plain description, the model's reasoning just before the call, the
inputs (secrets hidden), a short result, whether it worked, how long it took and, for browser and computer tools, a
small screenshot. The job's final reply is a row too.

Best effort, like the browser run log: nothing here may break or noticeably slow a job. Rows are written when a step
ends, in the background; ``Tracer.flush`` waits for them when the job's turn ends.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from langchain_core.callbacks import AsyncCallbackHandler

log = logging.getLogger(__name__)

MAX_STEPS = 300  # per job turn
MAX_SHOTS = 60
WHY_CHARS = 1200
RESULT_CHARS = 1500
ARG_CHARS = 300
ARGS_CHARS = 2000
SHOTS_KEEP_DAYS = 30
CURRENT: ContextVar["Tracer | None"] = ContextVar("job_tracer", default=None)

_SECRET_KEY = re.compile(r"pass(word|wd|code)?|secret|token|api[_-]?key|auth|cookie|credential|otp|pin\b|cvv|card", re.I)
_SECRET_VALUE = re.compile(r"\b(sk|pk|rk|ghp|gho|xox[abps]|AKIA)[-_A-Za-z0-9]{12,}|\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+")
# Tools that change or read the screen: the step gets a screenshot of the computer afterwards.
_SHOT_PREFIXES = ("browser_", "computer_")
_NO_SHOT = {"browser_logins", "browser_saved_runs", "browser_page_log", "browser_failed", "computer_status", "computer_missing"}
HIDDEN = "•••"


def _clip(text: Any, n: int) -> str:
    s = str(text if text is not None else "")
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def redact(value: Any, key: str = "", depth: int = 0) -> Any:
    """Inputs as shown in a trace: values under secret-looking keys and secret-looking strings are hidden, long
    strings clipped, deep structures cut short."""
    if key and _SECRET_KEY.search(key):
        return HIDDEN
    if isinstance(value, str):
        return _clip(_SECRET_VALUE.sub(HIDDEN, value), ARG_CHARS)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if depth >= 3:
        return "…"
    if isinstance(value, dict):
        return {str(k)[:60]: redact(v, str(k), depth + 1) for k, v in list(value.items())[:30]}
    if isinstance(value, (list, tuple)):
        return [redact(v, key, depth + 1) for v in list(value)[:20]]
    return _clip(value, ARG_CHARS)


def safe_args(args: dict) -> dict:
    out = redact(args or {})
    if not isinstance(out, dict):
        return {}
    if len(json.dumps(out, default=str)) > ARGS_CHARS:
        out = {k: (_clip(json.dumps(v, default=str), 120) if not isinstance(v, (int, float, bool)) else v) for k, v in out.items()}
    return out


def reasoning(message: Any) -> str:
    """What the model said or thought just before its tool calls: thinking blocks first, then its text."""
    content = getattr(message, "content", "")
    thought, said = [], []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, str):
                said.append(b)
            elif isinstance(b, dict):
                if b.get("type") in ("thinking", "reasoning"):
                    thought.append(str(b.get("thinking") or b.get("reasoning") or b.get("text") or ""))
                elif b.get("type") == "text":
                    said.append(str(b.get("text") or ""))
    else:
        said.append(str(content or ""))
    text = " ".join(t.strip() for t in thought + said if t and t.strip())
    return _clip(_SECRET_VALUE.sub(HIDDEN, re.sub(r"\s+", " ", text)).strip(), WHY_CHARS)


def outcome(output: Any) -> tuple[bool, str, str]:
    """(ok, error, result preview) of a tool's output (a ToolMessage, a string or a dict)."""
    status = getattr(output, "status", "")
    content = getattr(output, "content", output)
    if isinstance(content, list):
        content = " ".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    data: Any = content
    if isinstance(content, str):
        try:
            data = json.loads(content)
        except Exception:  # noqa: BLE001
            data = content
    ok, error = status != "error", ""
    if isinstance(data, dict):
        if data.get("ok") is False or (data.get("error") and not data.get("ok")):
            ok, error = False, _clip(data.get("error") or data.get("note") or "it did not work", 400)
        text = json.dumps(redact(data), default=str, ensure_ascii=False)
    else:
        text = _SECRET_VALUE.sub(HIDDEN, str(data or ""))
        if status == "error":
            error = _clip(text, 400)
    return ok, error, _clip(text, RESULT_CHARS)


def wants_shot(tool: str) -> bool:
    return tool.startswith(_SHOT_PREFIXES) and tool not in _NO_SHOT


class Tracer:
    """The trace of one job turn. Set as ``CURRENT`` by the job runner; every agent graph in the turn (the job's agent
    and teammates it asks) adds a ``TraceCallback`` that writes here."""

    def __init__(self, job, session_factory: Callable | None = None, agent_name: str = "") -> None:
        self.job_id = job.id
        self.user_id = job.user_id
        self.attempt = int(job.attempts or 0)
        self.agent_name = agent_name
        self.session_factory = session_factory
        self.count = 0
        self.shots = 0
        self.open: dict[str, dict] = {}
        self._pending: list[asyncio.Task] = []

    def _factory(self):
        if self.session_factory is None:
            from app.db import SessionLocal

            self.session_factory = SessionLocal
        return self.session_factory

    def start(self, run_id: Any, tool: str, action: str, why: str, args: dict, agent_name: str, depth: int) -> None:
        key = str(run_id or uuid.uuid4())
        seen = self.open.get(key)
        if seen is not None:
            # The same tool call reported by an outer graph's inherited callback and the teammate's own: the deeper
            # (more specific) one names the agent.
            if depth > seen["depth"]:
                seen.update(agent_name=agent_name or seen["agent_name"], depth=depth, why=why or seen["why"])
            return
        if self.count >= MAX_STEPS:
            return
        self.count += 1
        self.open[key] = {
            "i": self.count, "tool": tool[:120], "action": _clip(action or tool, 300), "why": why, "args": args,
            "agent_name": (agent_name or self.agent_name)[:80], "depth": depth, "t0": time.monotonic(),
            "started_at": datetime.now(timezone.utc),
        }  # fmt: skip

    def end(self, run_id: Any, output: Any = None, error: str = "") -> None:
        step = self.open.pop(str(run_id), None)
        if step is None:
            return
        if error:
            ok, err, result = False, _clip(error, 400), ""
        else:
            ok, err, result = outcome(output)
        step.update(ok=ok, error=err, result=result, ms=int((time.monotonic() - step.pop("t0")) * 1000))
        shot = wants_shot(step["tool"]) and self.shots < MAX_SHOTS
        if shot:
            self.shots += 1
        self._pending.append(asyncio.create_task(self._save(step, shot)))

    def reply(self, text: str, agent_name: str = "") -> None:
        if self.count >= MAX_STEPS or not text:
            return
        self.count += 1
        step = {
            "i": self.count, "kind": "reply", "tool": "", "action": "Replied", "why": "", "args": {},
            "agent_name": (agent_name or self.agent_name)[:80], "ok": True, "error": "",
            "result": _clip(_SECRET_VALUE.sub(HIDDEN, text), RESULT_CHARS), "ms": 0,
            "started_at": datetime.now(timezone.utc),
        }  # fmt: skip
        self._pending.append(asyncio.create_task(self._save(step, False)))

    async def _shot(self, step: dict) -> str:
        try:
            from app.browser_runs.runlog import latest_frame, thumbnail
            from app.storage import put_bytes

            png = await latest_frame(self.user_id)
            jpg = await asyncio.to_thread(thumbnail, png) if png else b""
            if not jpg:
                return ""
            key = f"{self.user_id}/job-traces/{self.job_id}/{self.attempt}-{step['i']}.jpg"
            await asyncio.to_thread(put_bytes, key, jpg, "image/jpeg")
            return key
        except Exception as exc:  # noqa: BLE001
            log.info("trace screenshot skipped: %s", type(exc).__name__)
            return ""

    async def _browser_run(self, db, step: dict):
        """The browser run a browser_task step recorded (it is saved before the tool returns)."""
        from sqlalchemy import select

        from app.models import BrowserRun

        since = step["started_at"] - timedelta(seconds=2)
        q = (
            select(BrowserRun.id)
            .where(BrowserRun.user_id == self.user_id, BrowserRun.started_at >= since)
            .order_by(BrowserRun.started_at.desc())
            .limit(1)
        )
        return (await db.execute(q)).scalar_one_or_none()

    async def _save(self, step: dict, shot: bool) -> None:
        try:
            from app.models import JobTraceStep

            key = await self._shot(step) if shot else ""
            async with self._factory()() as db:
                run_id = await self._browser_run(db, step) if step["tool"] == "browser_task" else None
                db.add(JobTraceStep(
                    job_id=self.job_id, user_id=self.user_id, attempt=self.attempt, i=step["i"],
                    agent_name=step["agent_name"], kind=step.get("kind", "tool"), tool=step["tool"],
                    action=step["action"], why=step["why"], args=step["args"], result=step["result"], ok=step["ok"],
                    error=step["error"], ms=step["ms"], shot=key, browser_run_id=run_id, started_at=step["started_at"],
                ))  # fmt: skip
                await db.commit()
        except Exception as exc:  # noqa: BLE001
            log.info("trace step not saved: %s", type(exc).__name__)

    async def flush(self, timeout: float = 15.0) -> None:
        """Wait for steps still being written; a step still open when the turn ends (cut off) is saved as stopped."""
        for key in list(self.open):
            self.end(key, error="Stopped before it finished")
        if self._pending:
            try:
                await asyncio.wait_for(asyncio.gather(*self._pending, return_exceptions=True), timeout)
            except Exception:  # noqa: BLE001
                pass
            self._pending = []


class TraceCallback(AsyncCallbackHandler):
    """LangChain events of one agent graph into the current job's trace."""

    raise_error = False

    def __init__(self, tracer: Tracer, agent_name: str = "", depth: int = 0, connector_names: dict | None = None):
        self.tracer = tracer
        self.agent_name = agent_name
        self.depth = depth
        self.connector_names = connector_names or {}
        self.why = ""

    async def on_chat_model_start(self, serialized, messages, **kwargs) -> None:  # noqa: ANN001
        self.why = ""

    async def on_llm_end(self, response, **kwargs) -> None:  # noqa: ANN001
        try:
            msg = getattr(response.generations[-1][-1], "message", None)
        except Exception:  # noqa: BLE001
            msg = None
        if msg is None:
            return
        if getattr(msg, "tool_calls", None):
            self.why = reasoning(msg)
        # The reply step comes from the job's result (run_job), not from here: model calls made inside tools reach
        # this callback too and would look like replies.

    async def on_tool_start(self, serialized, input_str, *, run_id=None, inputs=None, **kwargs) -> None:  # noqa: ANN001
        from app.agent.progress import _parse_inputs, describe_tool

        name = (serialized or {}).get("name") or kwargs.get("name") or ""
        args = _parse_inputs(input_str, inputs)
        try:
            action = describe_tool(name, args, self.connector_names) or name
        except Exception:  # noqa: BLE001
            action = name
        self.tracer.start(run_id, name, action, self.why, safe_args(args), self.agent_name, self.depth)

    async def on_tool_end(self, output, *, run_id=None, **kwargs) -> None:  # noqa: ANN001
        self.tracer.end(run_id, output)

    async def on_tool_error(self, error, *, run_id=None, **kwargs) -> None:  # noqa: ANN001
        self.tracer.end(run_id, error=f"{type(error).__name__}: {error}")


def callbacks(agent_name: str = "", depth: int = 0, connector_names: dict | None = None) -> list:
    tracer = CURRENT.get()
    return [TraceCallback(tracer, agent_name, depth, connector_names)] if tracer is not None else []


# ---------------------------------------------------------------- reading


def _shot_url(key: str) -> str:
    from app.storage import file_url

    return file_url(key, key.rsplit("/", 1)[-1], "image/jpeg", inline=True)


def describe_step(row) -> dict:
    return {
        "id": row.id, "job_id": str(row.job_id), "attempt": row.attempt, "i": row.i, "agent_name": row.agent_name,
        "kind": row.kind, "tool": row.tool, "action": row.action, "why": row.why, "args": row.args or {},
        "result": row.result, "ok": row.ok, "error": row.error, "ms": row.ms,
        "shot_url": _shot_url(row.shot) if row.shot else None,
        "browser_run_id": str(row.browser_run_id) if row.browser_run_id else None,
        "at": row.started_at.isoformat() if row.started_at else None,
    }  # fmt: skip


async def tree_trace(db, user, root_id) -> dict:
    """The trace of a job and every job it delegated to, in the order the steps started."""
    from sqlalchemy import or_, select

    from app.models import AgentJob, JobTraceStep

    jobs = (
        await db.execute(
            select(AgentJob)
            .where(AgentJob.user_id == user.id, or_(AgentJob.id == root_id, AgentJob.root_id == root_id))
            .order_by(AgentJob.created_at)
        )
    ).scalars().all()
    ids = [j.id for j in jobs]
    rows = []
    if ids:
        rows = (
            await db.execute(
                select(JobTraceStep)
                .where(JobTraceStep.user_id == user.id, JobTraceStep.job_id.in_(ids))
                .order_by(JobTraceStep.started_at, JobTraceStep.id)
                .limit(1000)
            )
        ).scalars().all()
    return {
        "jobs": [
            {"id": str(j.id), "parent_id": str(j.parent_id) if j.parent_id else None, "title": j.title, "role": j.role,
             "agent_name": (j.meta or {}).get("agent_name") or "", "status": j.status}
            for j in jobs
        ],  # fmt: skip
        "steps": [describe_step(r) for r in rows],
    }


async def tidy_screenshots(session_factory, user_id, days: int = SHOTS_KEEP_DAYS) -> int:
    """Drop screenshots of trace steps older than ``days`` (the steps stay, without pictures)."""
    from sqlalchemy import select

    from app.models import JobTraceStep

    uid = uuid.UUID(str(user_id))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(JobTraceStep).where(JobTraceStep.user_id == uid, JobTraceStep.started_at < cutoff, JobTraceStep.shot != "")
            )
        ).scalars().all()
        keys = [r.shot for r in rows]
        for r in rows:
            r.shot = ""
        await db.commit()
    if keys:
        from app.storage import delete_keys

        try:
            await asyncio.to_thread(delete_keys, keys)
        except Exception as exc:  # noqa: BLE001
            log.info("old trace screenshots not deleted: %s", type(exc).__name__)
    return len(keys)
