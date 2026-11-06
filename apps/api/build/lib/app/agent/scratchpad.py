"""Per-chat working checklist. Not long-term memory."""

from __future__ import annotations

from app.models import ChatSession


def _items(ctx) -> list[dict]:
    pad = getattr(ctx, "scratchpad", None)
    if not isinstance(pad, list):
        ctx.scratchpad = []
    return ctx.scratchpad


def _next_id(items: list[dict]) -> str:
    n = 1
    used = {str(row.get("id") or "") for row in items}
    while f"s{n}" in used:
        n += 1
    return f"s{n}"


def _find(items: list[dict], item_id: str) -> dict | None:
    for row in items:
        if str(row.get("id")) == str(item_id):
            return row
    return None


async def load(ctx) -> list[dict]:
    items = _items(ctx)
    if items or ctx.db is None or ctx.session_id is None:
        return items
    try:
        sess = await ctx.db.get(ChatSession, ctx.session_id)
    except Exception:  # noqa: BLE001
        return items
    if sess is None or not isinstance(getattr(sess, "state", None), dict):
        return items
    saved = sess.state.get("scratchpad")
    if isinstance(saved, list):
        ctx.scratchpad = [row for row in saved if isinstance(row, dict)]
    return ctx.scratchpad


async def save(ctx) -> None:
    if ctx.db is None or ctx.session_id is None:
        return
    sess = await ctx.db.get(ChatSession, ctx.session_id)
    if sess is None:
        return
    state = dict(sess.state or {})
    state["scratchpad"] = list(ctx.scratchpad or [])
    sess.state = state
    await ctx.db.commit()


async def listing(ctx) -> dict:
    items = await load(ctx)
    return {"items": items, "open": sum(1 for i in items if i.get("status") != "done")}


async def add(ctx, text: str) -> dict:
    body = " ".join((text or "").split())
    if not body:
        return {"error": "text is required"}
    items = await load(ctx)
    row = {"id": _next_id(items), "text": body[:400], "status": "open"}
    items.append(row)
    await save(ctx)
    return {"ok": True, "item": row, "items": items}


async def update(ctx, item_id: str, text: str = "", status: str = "") -> dict:
    items = await load(ctx)
    row = _find(items, item_id)
    if row is None:
        return {"error": f"unknown scratchpad item {item_id}", "items": items}
    if text:
        row["text"] = " ".join(text.split())[:400]
    if status in {"open", "done"}:
        row["status"] = status
    await save(ctx)
    return {"ok": True, "item": row, "items": items}


async def complete(ctx, item_id: str) -> dict:
    return await update(ctx, item_id, status="done")


async def remove(ctx, item_id: str) -> dict:
    items = await load(ctx)
    keep = [row for row in items if str(row.get("id")) != str(item_id)]
    if len(keep) == len(items):
        return {"error": f"unknown scratchpad item {item_id}", "items": items}
    ctx.scratchpad = keep
    await save(ctx)
    return {"ok": True, "items": keep}


def as_prompt(items: list[dict] | None) -> str:
    rows = [i for i in (items or []) if isinstance(i, dict) and i.get("text")]
    if not rows:
        return ""
    lines = []
    for row in rows:
        mark = "x" if row.get("status") == "done" else " "
        lines.append(f"- [{mark}] {row.get('id')}: {row.get('text')}")
    return "Current scratchpad:\n" + "\n".join(lines)
