"""One database session, safe for tool calls that run at the same time (docs/LLD.md "Parallel tool calls").

When the model asks for several tools in one step, the agent runs them at the same time. Every tool in a turn uses the
turn's one ``AsyncSession``, and a session can't run two statements at once: SQLAlchemy raises
``IllegalStateChangeError`` / asyncpg "another operation is in progress". So parallel connector calls failed, and the
prompt couldn't ask for them.

``SerialSession`` wraps the session: its database methods take turns behind one lock, while everything else a tool
does (the connector's network call, which is where the time goes) runs at the same time. Plain attributes and ``add``
pass straight through.
"""

from __future__ import annotations

import asyncio
from typing import Any

_SERIAL = frozenset({
    "execute", "scalar", "scalars", "get", "get_one", "commit", "flush", "refresh", "rollback", "merge", "delete",
    "stream", "stream_scalars", "run_sync", "connection", "close", "invalidate", "reset",
})  # fmt: skip


class SerialSession:
    """The turn's session, with its awaited methods run one at a time (re-entrant within one task)."""

    def __init__(self, session: Any):
        object.__setattr__(self, "_session", session)
        object.__setattr__(self, "_lock", asyncio.Lock())
        object.__setattr__(self, "_owner", None)

    @property
    def wrapped(self) -> Any:
        return self._session

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._session, name)
        if name not in _SERIAL or not callable(attr):
            return attr

        async def serial(*args: Any, **kwargs: Any) -> Any:
            task = asyncio.current_task()
            if self._owner is task:  # already holding it (a helper calling another): no deadlock
                return await attr(*args, **kwargs)
            async with self._lock:
                object.__setattr__(self, "_owner", task)
                try:
                    return await attr(*args, **kwargs)
                finally:
                    object.__setattr__(self, "_owner", None)

        return serial

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._session, name, value)

    def __bool__(self) -> bool:
        return True


def serial(session: Any) -> Any:
    """``session`` made safe for parallel tool calls; ``None`` and an already wrapped session are returned as they are."""
    if session is None or isinstance(session, SerialSession):
        return session
    return SerialSession(session)
