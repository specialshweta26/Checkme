"""Today board: what needs the user, who is working, what finished, and which routines are set."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from connector_core.text import clip

WORKING = {"queued", "running", "waiting_children"}
NEEDS_JOB = {"waiting_approval", "waiting_input"}
COMING = {"active", "running"}
DONE_JOB = {"done"}


def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz or "Asia/Kolkata")
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def _parse(value) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _local(value, tz: str) -> datetime | None:
    dt = _parse(value)
    if dt is None:
        return None
    return dt.astimezone(_zone(tz))


def _same_day(value, now: datetime, tz: str) -> bool:
    left = _local(value, tz)
    if left is None:
        return False
    return left.date() == now.astimezone(_zone(tz)).date()


def _clock(value, tz: str) -> str:
    local = _local(value, tz)
    if local is None:
        return ""
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{hour}:{local.strftime('%M %p')}"


def _date_label(now: datetime, tz: str) -> str:
    local = now.astimezone(_zone(tz))
    return f"{local.strftime('%A')}, {local.strftime('%B')} {local.day}"


def _greeting(now: datetime, tz: str) -> str:
    hour = now.astimezone(_zone(tz)).hour
    if hour < 12:
        return "Good morning"
    if hour < 17:
        return "Good afternoon"
    return "Good evening"


def _first_name(name: str, email: str = "") -> str:
    raw = (name or "").strip() or (email or "").split("@")[0]
    return raw.split()[0] if raw else "there"


def _href(agent_id: str | None, session_id: str | None = None, extra: str = "") -> str:
    parts = []
    if agent_id:
        parts.append(f"agent={agent_id}")
    if session_id:
        parts.append(f"c={session_id}")
    if extra:
        parts.append(extra)
    return "/chat" + (f"?{'&'.join(parts)}" if parts else "")


def _agent_map(agents: list[dict]) -> dict[str, dict]:
    return {str(a.get("id") or ""): a for a in agents if a.get("id")}


def _named(agents: dict[str, dict], agent_id, fallback: str = "") -> dict:
    row = agents.get(str(agent_id or "")) or {}
    fallback_name = (fallback or "").strip()
    if fallback_name.lower() == "lumio":
        fallback_name = ""
    return {
        "agent_id": str(row.get("id") or agent_id or "") or None,
        "agent_name": row.get("name") or fallback_name or "Agent",
        "agent_color": row.get("color") or "",
        "agent_avatar": row.get("avatar") or "",
        "agent_title": row.get("title") or "",
    }


def _who(agents: list[dict], by_agent: dict[str, dict], agent_id, agent_name: str = "", default_id: str | None = None) -> dict | None:
    """A living bot, or None when the bot was deleted. Nameless work stays with the default bot."""
    if agent_id and str(agent_id) in by_agent:
        return _named(by_agent, agent_id)
    label = (agent_name or "").strip()
    if label:
        if label.lower() == "lumio":
            return None
        for row in agents:
            if (row.get("name") or "").strip().lower() == label.lower():
                return _named(by_agent, row.get("id"), label)
        return None
    if not default_id or str(default_id) not in by_agent:
        return None
    return _named(by_agent, default_id)


def _recent(value, now: datetime, *, hours: int) -> bool:
    dt = _parse(value)
    if dt is None:
        return False
    return (now - dt).total_seconds() <= hours * 3600


def _approval_line(pending) -> str:
    if not pending or not isinstance(pending[0], dict):
        return ""
    title = str(pending[0].get("title") or "").strip()
    preview = str(pending[0].get("preview") or "").strip()
    if preview[:1] in "{[" or preview.startswith("{'"):
        preview = ""
    return title or preview


def _mail_owner(agents: list[dict], default_id: str | None) -> dict:
    for row in agents:
        blob = f"{row.get('name') or ''} {row.get('title') or ''} {row.get('focus') or ''}".lower()
        if "inbox" in blob or "mail" in blob:
            return row
    return _agent_map(agents).get(str(default_id or "")) or (agents[0] if agents else {})


def assemble(
    *,
    now: datetime,
    tz: str,
    name: str,
    email: str = "",
    agents: list[dict],
    jobs: list[dict],
    tasks: list[dict],
    replies: list[dict],
    alerts: list[dict],
    live: list[dict] | None = None,
) -> dict:
    """Turn already-loaded rows into the four Today columns. Times are the user's timezone."""
    by_agent = _agent_map(agents)
    default_id = next((str(a["id"]) for a in agents if a.get("kind") == "default"), None)
    if not default_id and agents:
        default_id = str(agents[0]["id"])
    mail = _mail_owner(agents, default_id)

    needs: list[dict] = []
    seen_need: set[str] = set()

    def add_need(key: str, item: dict) -> None:
        if not key or key in seen_need:
            return
        seen_need.add(key)
        needs.append(item)

    for job in jobs:
        if job.get("status") not in NEEDS_JOB:
            continue
        who = _who(agents, by_agent, job.get("agent_id"), job.get("agent_name") or "", default_id)
        if who is None:
            continue
        question = job.get("question") if isinstance(job.get("question"), dict) else {}
        pending = job.get("pending") if isinstance(job.get("pending"), list) else []
        ask = str((question or {}).get("question") or "").strip()
        card = _approval_line(pending)
        if ask:
            detail = f"Has a question for you · {clip(ask, 140)}"
        elif card:
            detail = f"Needs your approval · {clip(card, 140)}"
        else:
            detail = clip(job.get("result") or job.get("title") or "Needs you", 160)
        add_need(
            f"job:{job.get('id')}",
            {
                "id": f"job:{job.get('id')}",
                "kind": "job",
                **who,
                "title": who["agent_name"],
                "detail": detail,
                "at": job.get("created_at") or job.get("finished_at"),
                "when": "",
                "href": _href(who["agent_id"], job.get("session_id")),
            },
        )

    for alert in alerts:
        who = _named(by_agent, mail.get("id"), mail.get("name") or "")
        subject = clip(str(alert.get("subject") or alert.get("summary") or "A message needs a reply"), 120)
        reason = str(alert.get("alert_label") or "Needs a reply").strip()
        add_need(
            f"mail:{alert.get('id')}",
            {
                "id": f"mail:{alert.get('id')}",
                "kind": "alert",
                **who,
                "title": who["agent_name"],
                "detail": f"{reason} · {subject}",
                "at": alert.get("sort_at") or alert.get("date"),
                "when": "",
                "href": "/inbox",
            },
        )

    unread = {str(a.get("id")) for a in agents if a.get("unread")}
    for reply in replies:
        agent_id = str(reply.get("agent_id") or "")
        if not agent_id:
            continue
        choices = reply.get("choices") if isinstance(reply.get("choices"), dict) else {}
        ask = str((choices or {}).get("question") or "").strip()
        pending = reply.get("pending") if isinstance(reply.get("pending"), list) else []
        text = str(reply.get("content") or "").strip()
        fresh = _same_day(reply.get("created_at"), now, tz)
        needs_user = agent_id in unread or bool(ask) or bool(pending) or (fresh and "?" in text[:240])
        if not needs_user:
            continue
        if any(item.get("agent_id") == agent_id and item.get("kind") in ("job", "reply") for item in needs):
            continue
        who = _who(agents, by_agent, agent_id, "", None)
        if who is None:
            continue
        if ask:
            detail = f"Has a question for you · {clip(ask, 140)}"
        elif pending and isinstance(pending[0], dict):
            detail = f"Needs your approval · {clip(str(pending[0].get('title') or pending[0].get('preview') or 'an action'), 140)}"
        elif "?" in text[:240]:
            detail = f"Has a question for you · {clip(text, 140)}"
        else:
            detail = f"Has a reply for you · {clip(text, 140)}"
        add_need(
            f"reply:{agent_id}",
            {
                "id": f"reply:{agent_id}",
                "kind": "reply",
                **who,
                "title": who["agent_name"],
                "detail": detail,
                "at": reply.get("created_at"),
                "when": "",
                "href": _href(agent_id, reply.get("session_id")),
            },
        )

    working: list[dict] = []
    seen_work: set[str] = set()

    def add_work(key: str, item: dict) -> None:
        agent_key = str(item.get("agent_id") or key)
        if not agent_key or agent_key in seen_work:
            return
        seen_work.add(agent_key)
        working.append(item)

    for row in live or []:
        who = _who(agents, by_agent, row.get("agent_id"), row.get("agent_name") or "", default_id)
        if who is None or not _recent(row.get("created_at"), now, hours=6):
            continue
        add_work(
            f"live:{who['agent_id'] or row.get('id')}",
            {
                "id": f"live:{row.get('id')}",
                "kind": "live",
                **who,
                "title": who["agent_name"],
                "detail": clip(str(row.get("detail") or "Working on a reply"), 140),
                "at": row.get("created_at"),
                "when": "",
                "href": _href(who["agent_id"], row.get("session_id")),
            },
        )

    for job in jobs:
        if job.get("status") not in WORKING:
            continue
        if not _recent(job.get("started_at") or job.get("created_at"), now, hours=18):
            continue
        who = _who(agents, by_agent, job.get("agent_id"), job.get("agent_name") or "", default_id)
        if who is None:
            continue
        add_work(
            f"job:{job.get('id')}",
            {
                "id": f"job:{job.get('id')}",
                "kind": "job",
                **who,
                "title": who["agent_name"],
                "detail": clip(str(job.get("title") or job.get("prompt") or "Working"), 140),
                "at": job.get("started_at") or job.get("created_at"),
                "when": "",
                "href": _href(who["agent_id"], job.get("session_id")),
            },
        )

    for task in tasks:
        if task.get("status") != "running":
            continue
        who = _who(agents, by_agent, task.get("agent_id"), task.get("agent_name") or "", default_id)
        if who is None:
            continue
        if not who["agent_color"]:
            who["agent_color"] = task.get("agent_color") or ""
        add_work(
            f"task:{task.get('id')}",
            {
                "id": f"task:{task.get('id')}",
                "kind": "task",
                **who,
                "title": who["agent_name"],
                "detail": clip(str(task.get("title") or "Running a routine"), 140),
                "at": task.get("last_run_at"),
                "when": "",
                "href": "/tasks",
            },
        )

    done_job_tasks = {
        str(job.get("scheduled_task_id"))
        for job in jobs
        if job.get("status") in DONE_JOB and job.get("scheduled_task_id") and _same_day(job.get("finished_at"), now, tz)
    }
    done: list[dict] = []
    for job in jobs:
        if job.get("status") not in DONE_JOB or not _same_day(job.get("finished_at"), now, tz):
            continue
        who = _who(agents, by_agent, job.get("agent_id"), job.get("agent_name") or "", default_id)
        if who is None:
            continue
        done.append(
            {
                "id": f"job:{job.get('id')}",
                "kind": "job",
                **who,
                "title": clip(str(job.get("title") or "Finished task"), 140),
                "detail": who["agent_name"],
                "at": job.get("finished_at"),
                "when": "",
                "href": _href(who["agent_id"], job.get("session_id")),
            }
        )
    for task in tasks:
        if str(task.get("id") or "") in done_job_tasks:
            continue
        if not _same_day(task.get("last_run_at"), now, tz):
            continue
        if task.get("status") == "running":
            continue
        who = _who(agents, by_agent, task.get("agent_id"), task.get("agent_name") or "", default_id)
        if who is None:
            continue
        if not who["agent_color"]:
            who["agent_color"] = task.get("agent_color") or ""
        title = clip(str(task.get("title") or task.get("last_result") or "Finished routine"), 140)
        done.append(
            {
                "id": f"task:{task.get('id')}",
                "kind": "task",
                **who,
                "title": title,
                "detail": who["agent_name"],
                "at": task.get("last_run_at"),
                "when": "",
                "href": "/tasks",
            }
        )
    done.sort(key=lambda item: _parse(item.get("at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    for item in done:
        add_need(
            f"done:{item['id']}",
            {
                **item,
                "id": f"done:{item['id']}",
                "kind": "done",
                "title": item["agent_name"],
                "detail": f"Finished · {item['title']}",
            },
        )
    needs.sort(key=lambda item: _parse(item.get("at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    coming: list[dict] = []
    for task in tasks:
        if task.get("status") not in COMING:
            continue
        if not task.get("next_run_at") and not task.get("schedule_label"):
            continue
        who = _who(agents, by_agent, task.get("agent_id"), task.get("agent_name") or "", default_id)
        if who is None:
            continue
        if not who["agent_color"]:
            who["agent_color"] = task.get("agent_color") or ""
        coming.append(
            {
                "id": f"task:{task.get('id')}",
                "kind": "routine",
                **who,
                "title": clip(str(task.get("title") or task.get("schedule_label") or "Routine"), 140),
                "detail": who["agent_name"],
                "at": task.get("next_run_at"),
                "when": _clock(task.get("next_run_at"), tz) or str(task.get("schedule_label") or ""),
                "href": "/tasks",
            }
        )
    coming.sort(key=lambda item: _parse(item.get("at")) or datetime.max.replace(tzinfo=timezone.utc))

    roster = []
    ordered = sorted(agents, key=lambda a: (0 if a.get("kind") == "default" else 1, (a.get("name") or "").lower()))
    for row in ordered:
        if row.get("archived"):
            continue
        roster.append(
            {
                "id": str(row.get("id") or ""),
                "name": row.get("name") or "Agent",
                "title": row.get("title") or "",
                "color": row.get("color") or "",
                "avatar": row.get("avatar") or "",
            }
        )

    return {
        "greeting": _greeting(now, tz),
        "name": _first_name(name, email),
        "date_label": _date_label(now, tz),
        "timezone": tz or "Asia/Kolkata",
        "default_agent_id": default_id,
        "agents": roster,
        "counts": {
            "working": len(working),
            "needs_you": len(needs),
            "done_today": len(done),
            "coming_up": len(coming),
        },
        "needs_you": needs[:24],
        "working": working[:12],
        "done_today": done[:24],
        "coming_up": coming[:24],
    }
