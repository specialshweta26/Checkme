"""Reports when the event loop stalls, and what it was stuck on.

Chat, jobs, inbox and triggers share one event loop, so one blocking call freezes them all. A coroutine can't report
that, because it's frozen too, so a small thread watches a heartbeat the loop keeps updating.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback

log = logging.getLogger(__name__)

BEAT_S = 0.25
STALL_S = 2.0
CHECK_S = 0.5
REPEAT_S = 30.0  # one long stall logs once, then again only every so often


def stalled_stack(thread_id: int, limit: int = 8) -> str:
    frame = sys._current_frames().get(thread_id)
    return "".join(traceback.format_stack(frame, limit=limit)) if frame else ""


def should_report(behind: float, last_report: float, now: float) -> bool:
    return behind >= STALL_S and now - last_report >= REPEAT_S


def _watch(state: dict, thread_id: int, stop: threading.Event) -> None:
    last_report = float("-inf")
    while not stop.wait(CHECK_S):
        now = time.monotonic()
        behind = now - state["beat"]
        if should_report(behind, last_report, now):
            last_report = now
            log.warning("event loop stalled for %.1fs, stuck in:\n%s", behind, stalled_stack(thread_id))


async def watch_loop() -> None:
    """Runs for the life of the app."""
    state = {"beat": time.monotonic()}
    stop = threading.Event()
    threading.Thread(target=_watch, args=(state, threading.get_ident(), stop), name="loop-watch", daemon=True).start()
    try:
        while True:
            state["beat"] = time.monotonic()
            await asyncio.sleep(BEAT_S)
    finally:
        stop.set()
