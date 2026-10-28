"""Lazy catalog over staff, schedule, coding jobs, and media tasks."""

from __future__ import annotations

from typing import Any

from app.agent.staff import STAFF_TOOLS

CATALOG: list[dict[str, Any]] = [
    {
        "name": "schedule_task",
        "kind": "schedule",
        "blurb": "Queue work for later or on a repeating clock (in N minutes, tonight, every day).",
        "args": {"prompt": "full user request including when", "agent_name": "optional other agent"},
    },
    {
        "name": "list_scheduled_tasks",
        "kind": "schedule",
        "blurb": "List this agent's scheduled routines.",
        "args": {},
    },
    {
        "name": "update_scheduled_task",
        "kind": "schedule",
        "blurb": "Change or cancel a scheduled routine.",
        "args": {"id": "task id", "enabled": "optional bool"},
    },
    {
        "name": "today_briefing",
        "kind": "staff",
        "blurb": "Today's calendar, unread mail, and open commitments.",
        "args": {},
    },
    {
        "name": "ask_inbox",
        "kind": "staff",
        "blurb": "Answer a question from recent mail.",
        "args": {"question": "what to look for in the inbox"},
    },
    {
        "name": "scan_commitments",
        "kind": "staff",
        "blurb": "Scan mail for promises and due dates.",
        "args": {},
    },
    {
        "name": "list_commitments",
        "kind": "staff",
        "blurb": "List saved commitments.",
        "args": {},
    },
    {
        "name": "scan_admin",
        "kind": "staff",
        "blurb": "Scan mail for receipts, invoices, and admin to-dos.",
        "args": {},
    },
    {
        "name": "create_automation",
        "kind": "staff",
        "blurb": "Save a standing automation.",
        "args": {"prompt": "what should run and when"},
    },
    {
        "name": "list_automations",
        "kind": "staff",
        "blurb": "List saved automations.",
        "args": {},
    },
    {
        "name": "learn_voice",
        "kind": "staff",
        "blurb": "Learn how this user writes from sent mail.",
        "args": {},
    },
    {
        "name": "start_coding_job",
        "kind": "coding",
        "blurb": "Start a background coding job (clone, edit, test, PR, deploy) with a live timeline.",
        "args": {"task": "repo, goal, acceptance criteria", "title": "optional short title"},
    },
    {
        "name": "generate_image",
        "kind": "media",
        "blurb": "Generate an image from a prompt.",
        "args": {"prompt": "what to draw", "size": "1024x1024 | 1024x1792 | 1792x1024"},
    },
    {
        "name": "generate_video",
        "kind": "media",
        "blurb": "Generate a video (returns not-configured until a provider is set).",
        "args": {"prompt": "what to generate"},
    },
]


def search(query: str, limit: int = 8) -> list[dict]:
    q = (query or "").strip().lower()
    scored: list[tuple[int, dict]] = []
    for row in CATALOG:
        hay = f"{row['name']} {row['kind']} {row['blurb']}".lower()
        score = 0
        if not q:
            score = 1
        else:
            if q in hay:
                score += 3
            score += sum(1 for w in q.split() if len(w) > 2 and w in hay)
        if score:
            scored.append((score, {k: row[k] for k in ("name", "kind", "blurb")}))
    scored.sort(key=lambda item: (-item[0], item[1]["name"]))
    return [row for _, row in scored[: max(1, min(int(limit or 8), 20))]]


def load(name: str) -> dict:
    key = (name or "").strip()
    row = next((item for item in CATALOG if item["name"] == key), None)
    if row is None:
        return {"error": f"unknown task {key}", "available": [item["name"] for item in CATALOG]}
    return dict(row)


async def execute(ctx, name: str, args: dict | None = None) -> Any:
    from app.agent.deep import run_staff, start_coding_job
    from app.agent.web_tools import generate_image, generate_video

    key = (name or "").strip()
    payload = dict(args or {})
    row = load(key)
    if row.get("error"):
        return row
    if key == "generate_image":
        return await generate_image(str(payload.get("prompt") or ""), str(payload.get("size") or "1024x1024"))
    if key == "generate_video":
        return generate_video(str(payload.get("prompt") or ""))
    if key == "start_coding_job":
        from app.agent.coding import is_coding_agent

        if not is_coding_agent(ctx.agent):
            return {"error": "start_coding_job is for a coding agent. Ask a coding teammate, or switch to Coder."}
        return await start_coding_job(ctx, str(payload.get("task") or ""), str(payload.get("title") or ""))
    if key in STAFF_TOOLS or key in {"schedule_task", "list_scheduled_tasks", "update_scheduled_task"}:
        return await run_staff(ctx, key, payload)
    return {"error": f"cannot execute {key}"}
