"""Screenshots of the page a task is working on, saved into the chat as evidence.

Only the browser page is captured, never the rest of the user's screen. Payment pages are skipped: card details don't
belong in a stored image.
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

MAX_SHOTS = 15
_WANTS = re.compile(r"\b(screenshots?|screen\s?shots?|evidence)\b", re.I)


def wants_shots(goal: str) -> bool:
    return bool(_WANTS.search(goal or ""))


def shot_name(number: int, label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (label or "page").lower()).strip("-")[:40] or "page"
    return f"screenshot-{number:02d}-{slug}.png"


async def _png(ctx: Any, state: dict) -> bytes:
    from app.agent.computer_harness import computer_slug
    from app.desktop.agent import take_page_shot
    from connector_core import BinaryResult

    key = state.get("page_shot_key")
    if key:
        return take_page_shot(str(key))
    path = state.get("screenshot_path")
    slug = computer_slug(ctx)
    conn = ctx.connectors.get(slug) if slug and hasattr(ctx.connectors, "get") else None
    if path and conn is not None:
        blob = await conn.call("publish_file", {"path": path, "filename": "shot.png"})
        if isinstance(blob, BinaryResult):
            return bytes(blob.data or b"")
    return b""


async def capture(ctx: Any, label: str = "") -> dict:
    """Save the current page to this chat. Returns the attachment's details, or {"ok": False, "error": ...}."""
    from app.agent.attachments import store_blob
    from app.agent.computer_harness import run_computer
    from app.agent.fast_browser import _on_payment_page
    from connector_core import BinaryResult

    taken = int(getattr(ctx, "_page_shots", 0) or 0)
    if taken >= MAX_SHOTS:
        return {"ok": False, "error": f"Already saved {MAX_SHOTS} screenshots in this turn."}
    state = await run_computer(ctx, "browse_screenshot", {})
    if not isinstance(state, dict) or state.get("ok") is False:
        return {"ok": False, "error": str((state or {}).get("error") if isinstance(state, dict) else "no page")[:200] or "no page"}
    if _on_payment_page(state):
        if state.get("page_shot_key"):
            from app.desktop.agent import take_page_shot

            take_page_shot(str(state["page_shot_key"]))
        return {"ok": False, "skipped": "payment page", "error": "Not saved: this is a payment page and could show card details."}
    png = await _png(ctx, state)
    if not png:
        return {"ok": False, "error": "This browser did not return a screenshot."}
    name = shot_name(taken + 1, label or str(state.get("title") or ""))
    card = await store_blob(ctx.user.id, BinaryResult(filename=name, content_type="image/png", data=png))
    ctx.attachments.append(card)
    ctx._page_shots = taken + 1
    return {"ok": True, "filename": name, "preview_url": card.get("preview_url"), "download_url": card.get("download_url"),
            "url": str(state.get("url") or "")[:300], "title": str(state.get("title") or "")[:120],
            "note": "Attached to your reply automatically. Refer to it by what it shows; don't write its link."}  # fmt: skip
