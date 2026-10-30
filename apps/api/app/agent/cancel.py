"""Stopping a running chat turn or background job (the Stop button, "/stop" on WhatsApp, interrupt-and-redirect).

Two mechanisms, used together:

- A cooperative flag. ``check()`` is called between tool calls (``call_user_tool``, ``run_computer``, and a LangChain
  callback on every model/tool start) and raises ``TurnCancelled`` once the run was stopped, so the agent loop ends
  at the next step boundary even if the task-level cancel was swallowed somewhere.
- ``asyncio.Task.cancel()`` on the registered task, which interrupts whatever await is in flight (a model call, an
  HTTP request, a computer command). A cancelled cloud shell command is also killed on the computer (see
  ``computer_harness.cloud_exec``); the sandbox itself is never shut down.

Runs are registered per process. For a turn running in another API process, ``request_remote`` leaves a short-lived
Redis flag that the run's ``watch`` task polls; jobs are also stopped across processes by the worker's status watch.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

REMOTE_TTL_S = 600
WATCH_EVERY_S = 2.0


class TurnCancelled(Exception):
    """Raised at a step boundary after the user stopped the run."""


@dataclass
class Run:
    key: str
    user_id: str
    kind: str = "turn"  # turn | job
    session_id: str = ""
    agent_id: str = ""
    request_id: str = ""
    progress_id: str = ""
    job_id: str = ""
    task: asyncio.Task | None = None
    started: float = field(default_factory=time.time)
    cancelled: bool = False
    reason: str = ""
    extra: dict = field(default_factory=dict)  # e.g. the request text and, once stopped, its partial reply/steps

    def cancel(self, reason: str = "stopped") -> bool:
        first = not self.cancelled
        self.cancelled = True
        self.reason = self.reason or reason
        task = self.task
        if task is not None and not task.done() and task is not _current_task():
            task.cancel()
        return first


_RUNS: dict[str, Run] = {}
current: contextvars.ContextVar[Run | None] = contextvars.ContextVar("checkme_run", default=None)


def _current_task() -> asyncio.Task | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _s(value: Any) -> str:
    return str(value) if value else ""


def register(key: str, *, user_id: Any, kind: str = "turn", task: asyncio.Task | None = None, **info: Any) -> Run:
    run = Run(key=key, user_id=_s(user_id), kind=kind, task=task, **{k: _s(v) for k, v in info.items()})
    _RUNS[key] = run
    return run


def unregister(key: str, run: Run | None = None) -> None:
    if run is None or _RUNS.get(key) is run:
        _RUNS.pop(key, None)


def get(key: str) -> Run | None:
    return _RUNS.get(key)


def job_key(job_id: Any) -> str:
    return f"job:{job_id}"


def find(user_id: Any, *, session_id: Any = None, agent_id: Any = None, request_id: Any = None, progress_id: Any = None, kind: str | None = None) -> list[Run]:
    uid = _s(user_id)
    out = []
    for run in list(_RUNS.values()):
        if run.user_id != uid or (kind and run.kind != kind):
            continue
        if request_id and run.request_id != _s(request_id):
            continue
        if progress_id and run.progress_id != _s(progress_id):
            continue
        if session_id and run.session_id != _s(session_id):
            continue
        if agent_id and run.agent_id and run.agent_id != _s(agent_id):
            continue
        out.append(run)
    return out


def cancel_runs(runs: list[Run], reason: str = "stopped") -> list[Run]:
    for run in runs:
        run.cancel(reason)
    return runs


def cancel_job(job_id: Any, reason: str = "stopped") -> bool:
    run = _RUNS.get(job_key(job_id))
    if run is None:
        return False
    run.cancel(reason)
    return True


def is_cancelled(run: Run | None = None) -> bool:
    run = run or current.get()
    return bool(run is not None and run.cancelled)


def check() -> None:
    """Raise ``TurnCancelled`` if the current run was stopped. Call between steps."""
    run = current.get()
    if run is not None and run.cancelled:
        raise TurnCancelled(run.reason or "stopped")


# ---------------------------------------------------------------- cross-process flag (best effort)


def _remote_key(user_id: Any, ref: Any) -> str:
    return f"cancel:{user_id}:{ref}"


async def request_remote(user_id: Any, refs: list[Any], redis_client: Any = None) -> bool:
    try:
        if redis_client is None:
            from app.cache import get_redis

            redis_client = get_redis()
        for ref in refs:
            if ref:
                await redis_client.set(_remote_key(user_id, ref), b"1", ex=REMOTE_TTL_S)
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("remote cancel flag failed: %s", type(exc).__name__)
        return False


async def remote_requested(run: Run, redis_client: Any = None) -> bool:
    try:
        if redis_client is None:
            from app.cache import get_redis

            redis_client = get_redis()
        for ref in (run.request_id, run.progress_id, run.session_id):
            if ref and await redis_client.get(_remote_key(run.user_id, ref)):
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


async def clear_remote(run: Run, redis_client: Any = None) -> None:
    try:
        if redis_client is None:
            from app.cache import get_redis

            redis_client = get_redis()
        for ref in (run.request_id, run.progress_id, run.session_id):
            if ref:
                await redis_client.delete(_remote_key(run.user_id, ref))
    except Exception:  # noqa: BLE001
        pass


async def watch(run: Run, every_s: float = WATCH_EVERY_S, redis_client: Any = None) -> None:
    """Poll the Redis flag for a run started in this process but stopped from another one."""
    failures = 0
    while not run.cancelled:
        await asyncio.sleep(every_s)
        try:
            if await remote_requested(run, redis_client):
                run.cancel("stopped")
                return
            failures = 0
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            failures += 1
            if failures >= 3:
                return


# ---------------------------------------------------------------- LangChain hook


def callback():
    """A callback that raises ``TurnCancelled`` at the next model or tool start once the run is stopped."""
    from langchain_core.callbacks import AsyncCallbackHandler

    class CancelCallback(AsyncCallbackHandler):
        raise_error = True

        def __init__(self, run: Run | None):
            self.run = run

        def _check(self) -> None:
            if self.run is not None and self.run.cancelled:
                raise TurnCancelled(self.run.reason or "stopped")

        async def on_chat_model_start(self, serialized, messages, **kwargs) -> None:  # noqa: ANN001
            self._check()

        async def on_llm_start(self, serialized, prompts, **kwargs) -> None:  # noqa: ANN001
            self._check()

        async def on_tool_start(self, serialized, input_str, **kwargs) -> None:  # noqa: ANN001
            self._check()

    return CancelCallback(current.get())


def stopped_text(partial: str = "", steps: list[str] | None = None) -> str:
    body = (partial or "").strip()
    done = [s for s in (steps or []) if s][-6:]
    lines = []
    if body:
        lines.append(body)
    elif done:
        lines.append("Progress before stopping:\n" + "\n".join(f"- {s}" for s in done))
    lines.append("⏹ Stopped. Send a new message, or tap Continue to pick up where I left off.")
    return "\n\n".join(lines)


def resume_prompt(original: str, partial: str = "", steps: list[str] | None = None, *, interrupted_by: str = "") -> str:
    """Context for a fresh turn that continues (or is redirected from) a stopped one."""
    done = [s for s in (steps or []) if s][-12:]
    parts = []
    if interrupted_by:
        parts.append("The user interrupted your previous turn with this new message; follow the new message.")
    else:
        parts.append("The user stopped your previous turn and now wants you to continue it from where it stopped.")
    parts.append(f"Previous request: {original[:1500]}")
    if done:
        parts.append("Steps already done (do not redo finished work; check state first if unsure):\n" + "\n".join(f"- {s}" for s in done))
    if partial.strip():
        parts.append(f"Partial reply so far:\n{partial.strip()[:2000]}")
    return "\n\n".join(parts)
