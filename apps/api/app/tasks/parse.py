from __future__ import annotations

import json
from datetime import timedelta

from app.llm import chat_complete
from connector_core.calendar import as_local, format_local, now_local

KINDS = {"once", "daily", "weekly"}


async def parse_schedule(prompt: str, tz: str) -> dict:
    now = now_local(tz)
    spec = (
        "Turn a natural-language work request into a timed task. "
        "The user timezone is IST (Asia/Kolkata, UTC+5:30) unless they name another zone. "
        f"Current local time: {now.isoformat()} ({format_local(now, tz)}).\n"
        "Return JSON only:\n"
        "{"
        '"title":"short title",'
        '"instruction":"what the assistant should do when the task fires (no schedule wording)",'
        '"schedule_kind":"once|daily|weekly",'
        '"next_run_at":"ISO-8601 datetime with offset",'
        '"schedule_label":"human schedule such as Every day · 10:00 AM",'
        '"timed":true'
        "}\n"
        "Rules: timed=false only when there is no clock or interval (for example 'when I get an email'). "
        "Use daily for every day/morning/evening/weekday. Use weekly for every week or a weekday name. "
        "Otherwise once. If they say in N minutes/hours, set next_run_at to that instant. "
        "If the computed time is already past for a repeating task, use the next future occurrence. "
        "Keep instruction complete: recipients, subject, and what to fetch or write.\n\n"
        f"Request:\n{prompt.strip()}"
    )
    raw = ""
    try:
        resp = await chat_complete(
            [{"role": "user", "content": spec}],
            max_tokens=500,
            response_format={"type": "json_object"},
        )
        raw = (resp.choices[0].message.content or "").strip()
    except Exception:
        raw = ""
    data = _json(raw)
    if data.get("timed") is False:
        return {"timed": False, "title": _title(prompt, data), "instruction": prompt.strip()}
    kind = str(data.get("schedule_kind") or "once").lower()
    if kind not in KINDS:
        kind = "once"
    instruction = (data.get("instruction") or prompt).strip()
    title = _title(prompt, data)
    nxt = _when(data.get("next_run_at"), tz, kind)
    label = (data.get("schedule_label") or "").strip() or _label(kind, nxt, tz)
    return {
        "timed": True,
        "title": title[:200],
        "instruction": instruction,
        "schedule_kind": kind,
        "next_run_at": nxt,
        "schedule_label": label[:120],
    }


def fallback_once(prompt: str, tz: str, minutes: int = 5) -> dict:
    nxt = now_local(tz) + timedelta(minutes=minutes)
    return {
        "timed": True,
        "title": _title(prompt, {}),
        "instruction": prompt.strip(),
        "schedule_kind": "once",
        "next_run_at": nxt,
        "schedule_label": f"One-time in {minutes} minutes",
    }


def _when(value, tz: str, kind: str):
    now = now_local(tz)
    try:
        dt = as_local(value, tz)
    except Exception:
        dt = now + timedelta(minutes=5)
    if dt <= now:
        if kind == "daily":
            dt = dt + timedelta(days=1)
        elif kind == "weekly":
            dt = dt + timedelta(days=7)
        else:
            dt = now + timedelta(minutes=5)
    return dt


def _label(kind: str, dt, tz: str) -> str:
    local = as_local(dt, tz)
    clock = local.strftime("%I:%M %p").lstrip("0")
    if kind == "daily":
        return f"Every day · {clock}"
    if kind == "weekly":
        return f"Every {local.strftime('%A')} · {clock}"
    mins = int((local - now_local(tz)).total_seconds() // 60)
    if 0 < mins <= 180:
        return f"One-time in {mins} minutes"
    return f"One-time · {format_local(local, tz)}"


def _title(prompt: str, data: dict) -> str:
    title = (data.get("title") or "").strip()
    if title:
        return title[:200]
    line = " ".join((prompt or "").strip().split())
    return (line[:80] or "Scheduled task")


def _json(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start : end + 1])
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}
    return {}
