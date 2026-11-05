"""One job at a time on a user's computer, taken at the first computer call and kept until the job's turn ends.

Jobs that never touch the computer (mail, connectors, web research) run alongside it. Chat turns don't take this lock.
The lock is per process: during a deploy overlap, a job on the old instance and one on the new can share the computer.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import weakref
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

_LOCKS: "weakref.WeakValueDictionary[object, asyncio.Lock]" = weakref.WeakValueDictionary()


class JobComputer:
    """The current job's claim on its user's computer."""

    def __init__(self, user_id: object, on_wait: Callable[[], Awaitable[None]] | None = None):
        lock = _LOCKS.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            _LOCKS[user_id] = lock
        self._lock = lock  # keeps the shared lock alive while this job exists
        self._gate = asyncio.Lock()  # parallel tool calls in one job must take the lock only once
        self._on_wait = on_wait
        self.held = False

    async def ensure(self) -> None:
        if self.held:
            return
        async with self._gate:
            if self.held:
                return
            if self._lock.locked() and self._on_wait is not None:
                try:
                    await self._on_wait()
                except Exception:  # noqa: BLE001 - a missed status line must not stop the job
                    log.debug("could not report the wait for the computer", exc_info=True)
            await self._lock.acquire()
            self.held = True

    def release(self) -> None:
        if self.held:
            self.held = False
            self._lock.release()


current: contextvars.ContextVar[JobComputer | None] = contextvars.ContextVar("job_computer", default=None)


async def ensure() -> None:
    """Wait for the computer if this call runs inside a job. Outside a job this does nothing."""
    claim = current.get()
    if claim is not None:
        await claim.ensure()
