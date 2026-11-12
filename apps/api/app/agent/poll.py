"""Refresh a page until something appears, in code.

Some sites take minutes to finish a job (a report, an export), and show it in a list you refresh. An agent doing this by
hand clicks Refresh, sees no change, and goes off to try something else. This clicks the refresh control at a steady pace
and stops as soon as the wanted text or control is on the page.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.agent.gather import find_control

log = logging.getLogger(__name__)

MAX_WAIT_S = 600
MIN_EVERY_S = 3


def _found(page: dict, until: str) -> str:
    want = until.casefold().strip()
    if not want:
        return ""
    if want in str(page.get("text") or "").casefold():
        return "text"
    for row in page.get("elements") or []:
        if isinstance(row, dict) and want in str(row.get("name") or "").casefold():
            return "control"
    return ""


async def poll(ctx: Any, click: str, until: str, every_s: int = 10, max_s: int = 300) -> dict:
    """Click the control named ``click`` every ``every_s`` seconds until ``until`` shows (as text or a control's name),
    for at most ``max_s`` seconds."""
    from app.agent.computer_harness import run_computer as call

    every = max(MIN_EVERY_S, int(every_s or 10))
    limit = max(every, min(int(max_s or 300), MAX_WAIT_S))
    loop = asyncio.get_running_loop()
    started = loop.time()
    rounds = 0
    ctx._gathering = True
    try:
        while True:
            page = await call(ctx, "browser_snapshot", {})
            if not isinstance(page, dict) or page.get("ok") is False:
                return {"ok": False, "error": str((page or {}).get("error") if isinstance(page, dict) else page)[:300]}
            how = _found(page, until)
            waited = int(loop.time() - started)
            if how:
                log.info("browser.poll found after rounds=%d waited_s=%d", rounds, waited)
                return {"ok": True, "found": True, "how": how, "rounds": rounds, "waited_s": waited, "url": page.get("url"),
                        "note": f"'{until}' is on the page now. Take a snapshot for its ref, then carry on."}  # fmt: skip
            if waited + every > limit:
                return {"ok": True, "found": False, "rounds": rounds, "waited_s": waited,
                        "note": f"'{until}' didn't appear in {waited} s. Tell the user it isn't ready yet; don't keep refreshing by hand."}  # fmt: skip
            control = find_control(page.get("elements") or [], (), click) or _named(page, click)
            if control is None:
                return {"ok": False, "found": False, "error": f"No control called '{click}' on this page. Take a snapshot and check its name."}
            await call(ctx, "browser_act", {"actions": [{"kind": "click", "ref": control.get("ref")}]})
            rounds += 1
            await asyncio.sleep(every)
    finally:
        ctx._gathering = False


def _named(page: dict, click: str) -> dict | None:
    """A control whose name contains the words, when no name matches exactly ("Refresh" for "Refresh list")."""
    want = click.casefold().strip()
    for row in page.get("elements") or []:
        if isinstance(row, dict) and not row.get("disabled") and want and want in str(row.get("name") or "").casefold():
            if str(row.get("role") or "") not in {"textbox", "searchbox", "combobox"}:
                return row
    return None
