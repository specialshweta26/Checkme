from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import chat_complete
from app.models import ThreadSummary
from connector_core.text import clip


async def summarize_thread(db: AsyncSession, connector_id, user_id, thread: dict, focus: str | None = None) -> dict:
    last_id = thread.get("last_message_id") or ""
    cached = (
        await db.execute(
            select(ThreadSummary).where(
                ThreadSummary.connector_id == connector_id,
                ThreadSummary.thread_id == thread.get("thread_id"),
            )
        )
    ).scalar_one_or_none()
    if cached and cached.last_message_id == last_id and not focus:
        return cached.summary

    messages = thread.get("messages") or []
    chunks = [messages[i : i + 12] for i in range(0, len(messages), 12)] or [[]]
    partials = []
    for chunk in chunks:
        text = "\n\n".join(f"From {m.get('from')} on {m.get('date')}:\n{m.get('body','')}" for m in chunk)
        prompt = (
            "Summarize this part of an email thread. Return JSON "
            "{decisions, open_questions, action_items:[{owner,item}], who_said_what:[{person,point}]}. "
            f"Focus: {focus or 'overall'}\n\n{clip(text, 9000)}"
        )
        resp = await chat_complete(
            [{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=700,
        )
        try:
            partials.append(json.loads(resp.choices[0].message.content or "{}"))
        except json.JSONDecodeError:
            partials.append({"raw": resp.choices[0].message.content})

    if len(partials) == 1:
        summary = partials[0]
    else:
        resp = await chat_complete(
            [
                {
                    "role": "user",
                    "content": "Merge these thread-chunk summaries into one JSON with the same keys.\n"
                    + json.dumps(partials)[:12000],
                }
            ],
            response_format={"type": "json_object"},
            max_tokens=800,
        )
        summary = json.loads(resp.choices[0].message.content or "{}")

    summary["subject"] = thread.get("subject")
    summary["message_count"] = thread.get("message_count")
    if not focus:
        if cached:
            cached.last_message_id = last_id
            cached.summary = summary
        else:
            db.add(
                ThreadSummary(
                    connector_id=connector_id,
                    thread_id=thread.get("thread_id"),
                    user_id=user_id,
                    last_message_id=last_id,
                    summary=summary,
                )
            )
        await db.commit()
    return summary
