"""WhatsApp channel: linking a number, routing a message to an agent, and replying.

Flow: webhook (routes.py) -> ``handle_inbound`` in the background -> a chat job on the existing agent_jobs worker
(full agent: skills, computer, memory, task state) in a per (user, agent, whatsapp) ChatSession, so the thread
also shows in the app. The job runner calls ``after_run`` and ``JobProgress`` calls ``progress`` to reply.

Security: only a linked number reaches a user's agents (unknown numbers just get "connect from the app");
everything is scoped by the link's user_id; approvals are only accepted from a number linked to the job's user."""

from __future__ import annotations

import difflib
import logging
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.formatting import LIMIT, chunk, to_whatsapp
from app.channels.provider import BUTTON_MAX, Inbound, SendError, e164, get_provider
from app.config import get_settings
from app.models import (
    AgentGroup,
    AgentJob,
    AgentSkill,
    ChannelInbound,
    ChannelLink,
    ChannelLinkCode,
    ChatMessage,
    ChatSession,
    User,
    WorkspaceAgent,
)

log = logging.getLogger(__name__)

CHANNEL = "whatsapp"
CODE_TTL = timedelta(minutes=15)
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I
CODE_LEN = 6
WINDOW = timedelta(hours=24)
WINDOW_MARGIN = timedelta(minutes=5)
OUTBOX_MAX = 20
PROGRESS_GAP_S = 90.0
PROGRESS_MAX = 4
HISTORY_MAX = 8

HELP = (
    "**Checkme on WhatsApp**\n"
    "Just type a message and your default agent handles it.\n\n"
    "• `tony: …` or `@tony …` asks a specific agent\n"
    "• `/use tony` keeps talking to Tony (`/use default` to reset)\n"
    "• `/agents` lists your agents and their skills\n"
    "• `/room name` talks to a group room (`/room off` to leave)\n"
    "• `/new` starts a fresh conversation\n"
    "• `stop` or `/stop` stops what the agent is doing (`continue` picks it up again)\n"
    "• `/unlink` disconnects this number\n"
    "Voice notes, photos and documents work too."
)
UNKNOWN = (
    "This number isn't connected to Checkme yet. Open Checkme → Connectors → Connect WhatsApp and send the "
    "link code shown there."
)
CHANNEL_NOTE = (
    "[The user is messaging from WhatsApp. Reply briefly in plain text (bold, italics, bullets and short code blocks "
    "are fine; no tables). Approvals reach them as buttons. Files you attach are sent to them.]"
)

_progress_state: dict[str, dict] = {}


def enabled() -> bool:
    return get_provider().enabled()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(ts: datetime | None) -> datetime | None:
    if ts is not None and ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def mask(address: str) -> str:
    """+919876543210 -> +91•••••3210 (for logs and the UI list)."""
    a = str(address or "")
    return a[:3] + "•" * max(0, len(a) - 7) + a[-4:] if len(a) > 7 else a


async def _limited(key: str, limit: int, window_s: int = 60) -> bool:
    try:
        from app.cache import rate_limited

        return await rate_limited(key, limit, window_s)
    except Exception:  # noqa: BLE001 - Redis down must not drop messages
        return False


# ---------------------------------------------------------------- linking


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))


def business_number() -> str:
    """E.164 of the shared business number of the active provider (Twilio sender or Meta number)."""
    p = get_provider()
    fn = getattr(p, "business_number", None)
    return (fn() if callable(fn) else "") or e164(get_settings().whatsapp_business_number)


def wa_link(code: str = "") -> str:
    digits = "".join(ch for ch in business_number() if ch.isdigit())
    if not digits:
        return ""
    return f"https://wa.me/{digits}" + (f"?text=link%20{code}" if code else "")


async def create_link_code(db: AsyncSession, user: User) -> dict:
    await db.execute(delete(ChannelLinkCode).where(ChannelLinkCode.user_id == user.id))
    await db.execute(delete(ChannelLinkCode).where(ChannelLinkCode.expires_at < _now()))
    code = new_code()
    expires = _now() + CODE_TTL
    db.add(ChannelLinkCode(code=code, user_id=user.id, channel=CHANNEL, expires_at=expires))
    await db.flush()
    return {
        "code": code,
        "message": f"link {code}",
        "expires_at": expires.isoformat(),
        "wa_link": wa_link(code),
        "business_number": business_number(),
    }


def serialize_link(link: ChannelLink, agents: dict | None = None) -> dict:
    agents = agents or {}
    return {
        "id": str(link.id),
        "channel": link.channel,
        "address": link.address,
        "display_name": link.display_name or "",
        "sticky_agent": agents.get(str(link.sticky_agent_id)) if link.sticky_agent_id else None,
        "linked_at": link.created_at.isoformat() if link.created_at else "",
        "last_inbound_at": link.last_inbound_at.isoformat() if link.last_inbound_at else "",
        "window_open": in_window(link),
        "queued": len(link.outbox or []),
    }


async def status(db: AsyncSession, user: User) -> dict:
    links = (
        (await db.execute(select(ChannelLink).where(ChannelLink.user_id == user.id, ChannelLink.channel == CHANNEL)))
        .scalars()
        .all()
    )
    names = {
        str(a.id): a.name
        for a in (await db.execute(select(WorkspaceAgent).where(WorkspaceAgent.user_id == user.id))).scalars().all()
    }
    return {
        "enabled": enabled(),
        "business_number": business_number(),
        "wa_link": wa_link(),
        "links": [serialize_link(link, names) for link in links],
    }


async def unlink(db: AsyncSession, user: User, link_id: uuid.UUID) -> bool:
    link = await db.get(ChannelLink, link_id)
    if link is None or link.user_id != user.id:
        return False
    await db.delete(link)
    return True


async def redeem(db: AsyncSession, address: str, code: str, name: str = "") -> ChannelLink | None:
    """'link CODE' from a number: a valid unexpired code links (or moves) that number to the code's user."""
    code = (code or "").strip().upper()
    if not re.fullmatch(rf"[{CODE_ALPHABET}]{{{CODE_LEN}}}", code):
        return None
    row = await db.get(ChannelLinkCode, code)
    if row is None or row.channel != CHANNEL:
        return None
    if (_aware(row.expires_at) or _now()) < _now():
        await db.delete(row)
        return None
    link = await find_link(db, address)
    if link is None:
        link = ChannelLink(user_id=row.user_id, channel=CHANNEL, address=address, display_name=name[:120], outbox=[])
        db.add(link)
    else:
        if link.user_id != row.user_id:
            link.sticky_agent_id = None
            link.room_id = None
            link.outbox = []
        link.user_id = row.user_id
        link.display_name = (name or link.display_name or "")[:120]
    link.last_inbound_at = _now()
    await db.delete(row)
    await db.flush()
    return link


async def find_link(db: AsyncSession, address: str) -> ChannelLink | None:
    return (
        await db.execute(select(ChannelLink).where(ChannelLink.channel == CHANNEL, ChannelLink.address == address))
    ).scalar_one_or_none()


async def claim_message(db: AsyncSession, message_id: str) -> bool:
    """True the first time a provider message id is seen (webhook retries are ignored)."""
    from sqlalchemy.dialects.postgresql import insert

    res = await db.execute(
        insert(ChannelInbound).values(message_id=message_id, channel=CHANNEL, created_at=_now()).on_conflict_do_nothing()
    )
    return bool(res.rowcount)


# ---------------------------------------------------------------- routing


STOP_WORDS = {"stop", "/stop", "/cancel", "stop it", "stop now"}


@dataclass
class Command:
    kind: str  # message | link | help | agents | use | room | new | unlink | stop
    arg: str = ""
    body: str = ""
    target: str = ""


_TARGET_COLON = re.compile(r"^\s*@?([A-Za-z][\w .'-]{0,39}?)\s*:\s+(.+)$", re.S)
_TARGET_AT = re.compile(r"^\s*@([A-Za-z][\w.'-]{0,39})[\s,:]+(.+)$", re.S)


def parse(text: str) -> Command:
    raw = (text or "").strip()
    low = raw.lower()
    m = re.fullmatch(r"link\s+([A-Za-z0-9]{4,12})", raw, re.I)
    if m:
        return Command("link", m.group(1).upper())
    if low in {"/help", "help", "/start", "?"}:
        return Command("help")
    if low in {"/agents", "agents"}:
        return Command("agents")
    if low in {"/new", "/reset"}:
        return Command("new")
    if low == "/unlink":
        return Command("unlink")
    if low.rstrip(".! ") in STOP_WORDS:
        return Command("stop")
    m = re.fullmatch(r"/use(?:\s+(.+))?", raw, re.I | re.S)
    if m:
        return Command("use", (m.group(1) or "").strip())
    m = re.fullmatch(r"/room(?:\s+(.+))?", raw, re.I | re.S)
    if m:
        return Command("room", (m.group(1) or "").strip())
    m = _TARGET_AT.match(raw) or _TARGET_COLON.match(raw)
    if m:
        return Command("message", body=m.group(2).strip(), target=m.group(1).strip())
    return Command("message", body=raw)


def match_name(items: list, name: str, attr: str = "name"):
    """Fuzzy, case-insensitive pick by name: exact, then a unique prefix/first word, then a close spelling."""
    want = (name or "").strip().lstrip("@").lower()
    if not want:
        return None
    names = [(a, str(getattr(a, attr, "") or "").lower()) for a in items if not getattr(a, "archived", False)]
    exact = [a for a, n in names if n == want]
    if exact:
        return exact[0]
    prefix = [a for a, n in names if n.startswith(want) or n.split(" ")[0] == want]
    if len(prefix) == 1:
        return prefix[0]
    close = difflib.get_close_matches(want, [n for _, n in names], n=1, cutoff=0.75)
    if close:
        return next(a for a, n in names if n == close[0])
    return None


def in_window(link: ChannelLink, now: datetime | None = None) -> bool:
    last = _aware(link.last_inbound_at)
    if last is None:
        return False
    return (now or _now()) - last < WINDOW - WINDOW_MARGIN


# ---------------------------------------------------------------- sending


def text_parts(md: str) -> list[dict]:
    limit = int(getattr(get_provider(), "text_max", 0) or LIMIT)
    return [{"type": "text", "text": c} for c in chunk(to_whatsapp(md), limit)]


async def _send(address: str, part: dict) -> None:
    p = get_provider()
    kind = part.get("type")
    if kind == "buttons":
        await p.send_buttons(address, part["text"], [tuple(b) for b in part.get("buttons") or []])
    elif kind == "media":
        await p.send_media(address, part["url"], part.get("kind") or "document", part.get("caption") or "", part.get("filename") or "")
    else:
        await p.send_text(address, part.get("text") or "")


async def deliver(db: AsyncSession | None, link: ChannelLink, parts: list[dict], *, notify: str = "") -> str:
    """Send now inside the 24h window; outside it, hold the parts until the user writes again and, when a
    notification template is configured, send that template so they know something is waiting.

    Returns 'sent', 'queued' or 'failed'."""
    parts = [p for p in parts if p]
    if not parts or not enabled():
        return "failed"
    if in_window(link):
        ok = True
        for part in parts:
            try:
                await _send(link.address, part)
            except SendError as exc:
                ok = False
                log.warning("whatsapp send to %s failed: %s", mask(link.address), exc)
        return "sent" if ok else "failed"
    link.outbox = [*(link.outbox or []), *parts][-OUTBOX_MAX:]
    fn = getattr(get_provider(), "notify_template", None)
    template = (fn() if callable(fn) else get_settings().whatsapp_template_notify).strip()
    if template:
        summary = " ".join((notify or parts[0].get("text") or "You have a new update.").split())[:200]
        try:
            await get_provider().send_template(link.address, template, get_settings().whatsapp_template_lang, [summary])
        except SendError as exc:
            log.warning("whatsapp template to %s failed: %s", mask(link.address), exc)
    if db is not None:
        await db.flush()
    return "queued"


async def flush_outbox(link: ChannelLink) -> int:
    held = list(link.outbox or [])
    if not held:
        return 0
    link.outbox = []
    for part in held:
        try:
            await _send(link.address, part)
        except SendError as exc:
            log.warning("whatsapp outbox send to %s failed: %s", mask(link.address), exc)
    return len(held)


async def reply(link_or_address, text: str) -> None:
    """A direct answer to an inbound message (always inside the window)."""
    address = link_or_address.address if isinstance(link_or_address, ChannelLink) else str(link_or_address)
    for part in text_parts(text):
        try:
            await _send(address, part)
        except SendError as exc:
            log.warning("whatsapp reply to %s failed: %s", mask(address), exc)


# ---------------------------------------------------------------- inbound


async def handle_inbound(msg: Inbound, session_factory: Callable | None = None) -> str:
    """Process one accepted inbound message. Returns what happened (for tests and logs)."""
    if session_factory is None:
        from app.db import SessionLocal

        session_factory = SessionLocal
    async with session_factory() as db:
        try:
            outcome = await _handle(db, msg)
            await db.commit()
        except Exception:
            log.exception("whatsapp inbound from %s failed", mask(msg.address))
            await db.rollback()
            return "error"
    if outcome.startswith("job"):
        from app.jobs.worker import wake

        wake()
    return outcome


async def _handle(db: AsyncSession, msg: Inbound) -> str:
    s = get_settings()
    link = await find_link(db, msg.address)
    cmd = parse(msg.text) if msg.kind == "text" else Command("message", body=msg.text)
    if link is None:
        if cmd.kind == "link":
            if await _limited(f"wa-link:{msg.address}", 5, 600):
                await reply(msg.address, "Too many link attempts. Try again in a few minutes.")
                return "link_limited"
            link = await redeem(db, msg.address, cmd.arg, msg.name)
            if link is None:
                await reply(msg.address, "That link code is invalid or expired. Open Checkme → Connectors → Connect WhatsApp for a new one.")
                return "link_invalid"
            await reply(link, "✅ WhatsApp is connected to your Checkme account.\n\n" + HELP)
            return "linked"
        if not await _limited(f"wa-unknown:{msg.address}", 1, 3600):
            await reply(msg.address, UNKNOWN)
        return "unknown"

    if await _limited(f"wa:{msg.address}", max(1, s.whatsapp_rate_limit_per_minute), 60):
        if not await _limited(f"wa-slow:{msg.address}", 1, 60):
            await reply(link, "You're sending messages quickly; give me a minute.")
        return "rate_limited"
    link.last_inbound_at = _now()
    if msg.name and not link.display_name:
        link.display_name = msg.name[:120]
    await flush_outbox(link)
    user = await db.get(User, link.user_id)
    if user is None:
        return "no_user"

    if msg.kind == "button":
        return await _button(db, user, link, msg.button_id)
    if cmd.kind == "link":
        await reply(link, "This number is already connected. Send /help to see what you can do.")
        return "already_linked"
    if cmd.kind == "help":
        await reply(link, HELP)
        return "help"
    if cmd.kind == "agents":
        await reply(link, await agents_text(db, user, link))
        return "agents"
    if cmd.kind == "use":
        return await _use(db, user, link, cmd.arg)
    if cmd.kind == "room":
        return await _room(db, user, link, cmd.arg)
    if cmd.kind == "stop":
        return await stop_running(db, user, link)
    if cmd.kind == "unlink":
        await db.delete(link)
        await reply(msg.address, "WhatsApp is disconnected from Checkme. Link it again from Connectors any time.")
        return "unlinked"

    text, uploads, note = await _content(user, msg, cmd)
    if not text:
        await reply(link, note or "I can read text, voice notes, photos and documents. Send /help for commands.")
        return "empty"
    return await route_message(db, user, link, text, target=cmd.target, uploads=uploads, new_thread=cmd.kind == "new")


async def agents_text(db: AsyncSession, user: User, link: ChannelLink) -> str:
    from app.agents.service import ensure_default_agent

    chief = await ensure_default_agent(db, user)
    agents = [
        a
        for a in (await db.execute(select(WorkspaceAgent).where(WorkspaceAgent.user_id == user.id).order_by(WorkspaceAgent.created_at)))
        .scalars()
        .all()
        if not getattr(a, "archived", False)
    ]
    skills: dict[str, list[str]] = {}
    for name, agent_id in (await db.execute(select(AgentSkill.name, AgentSkill.agent_id).where(AgentSkill.user_id == user.id))).all():
        skills.setdefault(str(agent_id) if agent_id else "*", []).append(name)
    lines = ["**Your agents**"]
    for a in agents:
        tags = []
        if a.id == chief.id:
            tags.append("default")
        if link.sticky_agent_id == a.id:
            tags.append("current")
        head = f"• **{a.name}**" + (f" — {a.title}" if getattr(a, "title", "") else "") + (f" ({', '.join(tags)})" if tags else "")
        own = skills.get(str(a.id), [])
        lines.append(head + (f"\n   skills: {', '.join(own[:6])}" + (" …" if len(own) > 6 else "") if own else ""))
    if skills.get("*"):
        lines.append(f"Shared skills: {', '.join(skills['*'][:8])}")
    lines.append("\nSay `name: your request` or `/use name`.")
    return "\n".join(lines)


async def _agents(db: AsyncSession, user: User) -> list[WorkspaceAgent]:
    return list((await db.execute(select(WorkspaceAgent).where(WorkspaceAgent.user_id == user.id))).scalars().all())


async def _use(db: AsyncSession, user: User, link: ChannelLink, name: str) -> str:
    if not name:
        current = await db.get(WorkspaceAgent, link.sticky_agent_id) if link.sticky_agent_id else None
        await reply(link, f"You're talking to **{current.name}**." if current else "You're talking to your default agent. `/use name` to switch.")
        return "use_show"
    if name.lower() in {"default", "off", "none", "reset"}:
        link.sticky_agent_id = None
        await reply(link, "Back to your default agent.")
        return "use_reset"
    agent = match_name(await _agents(db, user), name)
    if agent is None:
        await reply(link, f"I couldn't find an agent called “{name[:40]}”. Send /agents to see them.")
        return "use_unknown"
    link.sticky_agent_id = agent.id
    link.room_id = None
    await reply(link, f"Now talking to **{agent.name}**. `/use default` to switch back.")
    return "use_set"


async def _room(db: AsyncSession, user: User, link: ChannelLink, name: str) -> str:
    if not name or name.lower() in {"off", "none", "leave"}:
        link.room_id = None
        await reply(link, "Left the room. Messages go to your agent again.")
        return "room_off"
    groups = list((await db.execute(select(AgentGroup).where(AgentGroup.user_id == user.id))).scalars().all())
    group = match_name(groups, name)
    if group is None:
        await reply(link, f"No room called “{name[:40]}”.")
        return "room_unknown"
    link.room_id = group.id
    await reply(link, f"Now in the room **{group.name}**. `/room off` to leave.")
    return "room_set"


async def _content(user: User, msg: Inbound, cmd: Command) -> tuple[str, list[dict], str]:
    """Text for the agent (+ uploads to show in the app); media is downloaded via Graph, voice is transcribed."""
    text = cmd.body if cmd.kind == "message" else ""
    if msg.kind in {"text", "button"} or not msg.media_id:
        return text, [], ""
    try:
        data, mime = await get_provider().download_media(msg.media_id)
    except SendError as exc:
        return text, [], f"I couldn't download that file ({exc})."
    mime = (msg.mime_type or mime or "application/octet-stream").split(";")[0].strip()
    if msg.kind == "audio":
        from app.skills.narration import transcribe

        ext = {"audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/aac": "m4a", "audio/amr": "amr"}.get(mime, "ogg")
        try:
            said = " ".join(seg.get("text", "") for seg in await transcribe(data, ext)).strip()
        except Exception as exc:  # noqa: BLE001
            log.info("whatsapp voice transcription failed: %s", type(exc).__name__)
            said = ""
        if not said:
            return text, [], "I couldn't make out that voice note. Could you type it?"
        return (f"{text}\n\n" if text else "") + f"(voice note) {said}", [], ""
    card = _store_upload(user, data, mime, msg.filename or _default_name(msg.kind, mime))
    extracted = (card.get("extracted_text") or "").strip()
    bits = f"- {card['filename']} (attachment_key={card['key']})" + (f"\n{extracted[:3000]}" if extracted else "")
    body = (text or ("Please look at this photo." if msg.kind == "image" else "Please review the attached file."))
    agent_text = body + "\n\nThe user sent these files on WhatsApp. Use the attachment_keys to open or forward them.\n" + bits
    return agent_text, [card], ""


def _default_name(kind: str, mime: str) -> str:
    ext = (mime.split("/")[-1] or "bin").split("+")[0][:8]
    return f"whatsapp-{kind}-{int(time.time())}.{ext}"


def _store_upload(user: User, data: bytes, mime: str, filename: str) -> dict:
    from app.agent.attachments import is_previewable
    from app.storage import extract_text, put_bytes, signed_url
    from connector_core.text import clip

    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)[:180] or "file"
    key = f"{user.id}/{uuid.uuid4().hex}/{safe}"
    put_bytes(key, data, mime)
    preview = is_previewable(safe, mime)
    kind = "pdf" if "pdf" in mime else "image" if mime.startswith("image/") else "file"
    return {
        "key": key,
        "filename": safe,
        "content_type": mime,
        "size": len(data),
        "kind": kind,
        "download_url": signed_url(key, safe, inline=False, content_type=mime),
        "preview_url": signed_url(key, safe, inline=True, content_type=mime) if preview else None,
        "previewable": preview,
        "extracted_text": clip(extract_text(safe, mime, data, limit=8000) or "", 4000),
    }


async def _thread(db: AsyncSession, user: User, agent: WorkspaceAgent, group: AgentGroup | None, fresh: bool) -> ChatSession:
    """The per (user, agent|room, whatsapp) chat thread; it also shows in the app under that agent."""
    stmt = select(ChatSession).where(ChatSession.user_id == user.id, ChatSession.state["channel"].astext == CHANNEL)
    stmt = stmt.where(ChatSession.group_id == group.id) if group is not None else stmt.where(
        ChatSession.agent_id == agent.id, ChatSession.group_id.is_(None)
    )
    sess = None if fresh else (await db.execute(stmt.order_by(ChatSession.updated_at.desc()).limit(1))).scalar_one_or_none()
    if sess is None:
        label = group.name if group is not None else agent.name
        sess = ChatSession(
            user_id=user.id,
            title=f"WhatsApp · {label}"[:200],
            state={"channel": CHANNEL},
            agent_id=None if group is not None else agent.id,
            group_id=group.id if group is not None else None,
        )
        db.add(sess)
        await db.flush()
    return sess


async def _recent_history(db: AsyncSession, sess: ChatSession) -> list[dict]:
    rows = (
        (
            await db.execute(
                select(ChatMessage).where(ChatMessage.session_id == sess.id).order_by(ChatMessage.id.desc()).limit(HISTORY_MAX)
            )
        )
        .scalars()
        .all()
    )
    return [{"role": m.role, "content": (m.content or "")[:1500]} for m in reversed(rows) if m.role in {"user", "assistant"}]


def _option_pick(job: AgentJob, text: str) -> str:
    opts = list(((job.meta or {}).get("question") or {}).get("options") or [])
    t = text.strip()
    if t.isdigit() and 1 <= int(t) <= len(opts):
        return opts[int(t) - 1]
    return next((o for o in opts if o.lower() == t.lower()), t)


async def _routed_agent(db: AsyncSession, user: User, link: ChannelLink) -> tuple[WorkspaceAgent, AgentGroup | None]:
    """The agent a plain message goes to: sticky agent, else the room's lead, else the default agent."""
    from app.agents.service import ensure_default_agent

    chief = await ensure_default_agent(db, user)
    agents = await _agents(db, user)
    if link.sticky_agent_id:
        agent = next((a for a in agents if a.id == link.sticky_agent_id and not getattr(a, "archived", False)), None)
        if agent is not None:
            return agent, None
    if link.room_id:
        group = await db.get(AgentGroup, link.room_id)
        if group is not None and group.user_id == user.id:
            from app.agents.collab import pick_owner
            from app.agents.service import get_group

            _, members = await get_group(db, user, group.id)
            return pick_owner(members, group) or chief, group
    return chief, None


async def stop_running(db: AsyncSession, user: User, link: ChannelLink) -> str:
    """"stop" / "/stop": cancel this user's running job (and its sub-jobs) for the routed agent. The computer stays up."""
    from app.agent import cancel as run_cancel
    from app.jobs import service as jobs

    agent, group = await _routed_agent(db, user, link)
    sess = await _thread(db, user, agent, group, fresh=False)
    stopped = await jobs.stop_chat_jobs(db, user.id, sess.id, agent_id=None if group is not None else agent.id)
    runs = run_cancel.cancel_runs(run_cancel.find(user.id, session_id=sess.id, kind="turn"), "stopped")
    name = group.name if group is not None else agent.name
    if stopped or runs:
        db.add(
            ChatMessage(
                session_id=sess.id, user_id=user.id, role="assistant", content="⏹ Stopped.",
                meta={"channel": CHANNEL, "stopped": True, "can_continue": True, "agent_id": str(agent.id), "agent_name": agent.name},
                request_id=f"wa-stop-{secrets.token_hex(4)}",
            )
        )
        await reply(link, f"⏹ Stopped **{name}**. Send *continue* to pick up where it left off, or just send a new message.")
        return "stopped"
    await reply(link, f"Nothing is running for **{name}** right now.")
    return "stop_idle"


CONTINUE_WORDS = {"continue", "resume", "go on", "carry on", "keep going"}


async def _last_stopped_job(db: AsyncSession, user_id, session_id, agent_id) -> AgentJob | None:
    row = (
        await db.execute(
            select(AgentJob)
            .where(AgentJob.user_id == user_id, AgentJob.session_id == session_id, AgentJob.agent_id == agent_id, AgentJob.parent_id.is_(None))
            .order_by(AgentJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row if row is not None and row.status == "cancelled" else None


async def route_message(
    db: AsyncSession,
    user: User,
    link: ChannelLink,
    text: str,
    *,
    target: str = "",
    uploads: list[dict] | None = None,
    new_thread: bool = False,
) -> str:
    """Pick the agent (target > sticky > room lead > default), then continue or start its job in the thread."""
    from app.agents.service import ensure_default_agent
    from app.jobs import service as jobs

    chief = await ensure_default_agent(db, user)
    agents = await _agents(db, user)
    agent = None
    group = None
    if target:
        agent = match_name(agents, target)
        if agent is None:
            text = f"{target}: {text}"  # "Note: buy milk" is a message, not a name
    if agent is None and link.sticky_agent_id:
        agent = next((a for a in agents if a.id == link.sticky_agent_id and not getattr(a, "archived", False)), None)
    if agent is None and link.room_id:
        group = await db.get(AgentGroup, link.room_id)
        if group is not None and group.user_id == user.id:
            from app.agents.collab import pick_owner
            from app.agents.service import get_group

            _, members = await get_group(db, user, group.id)
            agent = pick_owner(members, group) or chief
        else:
            link.room_id = None
            group = None
    agent = agent or chief
    if new_thread:
        await _thread(db, user, agent, group, fresh=True)
        await reply(link, f"Started a new conversation with **{group.name if group else agent.name}**.")
        return "new_thread"

    sess = await _thread(db, user, agent, group, fresh=False)
    sess.updated_at = _now()
    request_id = f"wa-{secrets.token_hex(6)}"
    user_meta: dict[str, Any] = {"channel": CHANNEL, "request_id": request_id}
    if uploads:
        user_meta["uploads"] = [{k: v for k, v in u.items() if k != "extracted_text"} for u in uploads]
    shown = text.split("\n\nThe user sent these files on WhatsApp.")[0]
    db.add(ChatMessage(session_id=sess.id, user_id=user.id, role="user", content=shown, meta=user_meta, request_id=request_id))
    channel = {"type": CHANNEL, "link_id": str(link.id), "address": link.address, "inbound_at": _now().isoformat(), "show_name": agent.id != chief.id}
    latest = await jobs.latest_chat_job(db, user.id, sess.id, agent.id)
    speaker = agent.name
    job = None
    ack = ""
    outcome = "job_new"
    if latest is not None and latest.status == "waiting_approval":
        word = text.strip().lower()
        pending = list((latest.meta or {}).get("pending") or [])
        if pending and word in {"approve", "yes", "ok", "approved", "go"}:
            return await _decide(db, link, latest, 0, approve=True)
        if pending and word in {"deny", "no", "decline", "cancel", "reject"}:
            return await _decide(db, link, latest, 0, approve=False)
    if latest is not None and latest.status in {"waiting_input", "queued", "running", "waiting_children"}:
        was = latest.status
        try:
            events = jobs.followup(latest, _option_pick(latest, text) if was == "waiting_input" else text)
        except jobs.JobError:
            events = None
        if events is not None:
            for ev in events:
                db.add(ev)
            job = latest
            outcome = "job_answer" if was == "waiting_input" else "job_followup"
            ack = "Got it, continuing." if was == "waiting_input" else f"Noted — {speaker} will pick that up next."
    if job is None:
        history = list((latest.meta or {}).get("history") or []) if latest is not None else []
        if not history:
            history = await _recent_history(db, sess)
            history = history[:-1] if history and history[-1].get("role") == "user" else history
        prompt = f"{CHANNEL_NOTE}\n\n{text}"
        if text.strip().lower().rstrip(".! ") in CONTINUE_WORDS:
            stopped = await _last_stopped_job(db, user.id, sess.id, agent.id)
            if stopped is not None:
                from app.agent.cancel import resume_prompt

                original = (stopped.prompt or "").replace(CHANNEL_NOTE, "").strip()
                prompt = f"{CHANNEL_NOTE}\n\n" + resume_prompt(original, stopped.result or "", [])
                history = history or list((stopped.meta or {}).get("history") or [])
        try:
            job = await jobs.create_job(
                db, user, agent=agent, prompt=prompt, title=shown[:120] or "WhatsApp message", role="worker", kind="chat",
                session_id=sess.id, history=history or None,
            )
        except jobs.JobError as exc:
            await reply(link, f"I couldn't start that: {exc}")
            return "job_error"
        ack = f"👍 On it — **{speaker}** is working on that." if channel["show_name"] else "👍 On it."
    meta = dict(job.meta or {})
    meta["channel"] = channel
    job.meta = meta
    await db.flush()
    info = jobs.serialize_job(job)
    db.add(
        ChatMessage(
            session_id=sess.id,
            user_id=user.id,
            role="assistant",
            content=ack.replace("*", ""),
            meta={
                "request_id": request_id, "route": "job", "job": info, "jobs": [info], "channel": CHANNEL,
                "speaker": {"id": str(agent.id), "name": agent.name, "color": getattr(agent, "color", "") or "", "title": getattr(agent, "title", "") or ""},
                "agent_id": str(agent.id), "agent_name": agent.name,
            },
            request_id=request_id,
        )
    )
    await reply(link, ack)
    return outcome


# ---------------------------------------------------------------- buttons (approvals and options)


def button_id(action: str, job_id, index: int) -> str:
    return f"wa:{action}:{job_id}:{index}"


def parse_button(value: str) -> tuple[str, uuid.UUID, int] | None:
    m = re.fullmatch(r"wa:(ap|dn|op):([0-9a-f-]{36}):(\d{1,2})", (value or "").strip())
    if not m:
        return None
    try:
        return m.group(1), uuid.UUID(m.group(2)), int(m.group(3))
    except ValueError:
        return None


async def _button(db: AsyncSession, user: User, link: ChannelLink, value: str) -> str:
    from app.jobs import service as jobs

    parsed = parse_button(value)
    if parsed is None:
        await reply(link, "That button has expired. Send /help to see what you can do.")
        return "button_unknown"
    action, job_id, index = parsed
    job = await db.get(AgentJob, job_id)
    if job is None or job.user_id != user.id:  # only a number linked to the job's own user can decide
        await reply(link, "That request isn't available.")
        return "button_denied"
    if action == "op":
        opts = list(((job.meta or {}).get("question") or {}).get("options") or [])
        if job.status != "waiting_input" or index >= len(opts):
            await reply(link, "That question was already answered.")
            return "option_stale"
        for ev in jobs.answer(job, opts[index]):
            db.add(ev)
        await _mark_channel(job, link)
        await reply(link, f"Got it: {opts[index]}")
        return "job_option"
    return await _decide(db, link, job, index, approve=action == "ap")


async def _decide(db: AsyncSession, link: ChannelLink, job: AgentJob, index: int, *, approve: bool) -> str:
    from app.jobs import service as jobs

    pending = list((job.meta or {}).get("pending") or [])
    if job.status != "waiting_approval" or index >= len(pending):
        await reply(link, "That approval was already handled.")
        return "approval_stale"
    card = pending[index]
    try:
        events = jobs.approve(job, card["token"]) if approve else jobs.decline(job, card["token"])
    except jobs.JobError as exc:
        await reply(link, str(exc))
        return "approval_error"
    for ev in events:
        db.add(ev)
    await _mark_channel(job, link)
    title = card.get("title") or "the action"
    await reply(link, f"✅ Approved: {title}" if approve else f"❌ Declined: {title}")
    return "job_approved" if approve else "job_declined"


async def _mark_channel(job: AgentJob, link: ChannelLink) -> None:
    meta = dict(job.meta or {})
    ch = dict(meta.get("channel") or {})
    ch.update({"type": CHANNEL, "link_id": str(link.id), "address": link.address, "inbound_at": _now().isoformat()})
    meta["channel"] = ch
    job.meta = meta


# ---------------------------------------------------------------- job -> WhatsApp


def _media_kind(card: dict) -> str:
    ctype = str(card.get("content_type") or "")
    if ctype in {"image/jpeg", "image/png"}:
        return "image"
    return "document"


def outcome_parts(job: AgentJob, agent_name: str = "", attachments: list | None = None) -> list[dict]:
    """What a finished job turn sends: the answer, approval buttons, question options, files."""
    meta = job.meta or {}
    ch = meta.get("channel") or {}
    head = f"**{agent_name}**\n" if ch.get("show_name") and agent_name else ""
    parts: list[dict] = []
    if job.status == "failed":
        return text_parts(f"{head}Sorry, that stopped with an error: {(job.error or 'unknown error')[:300]}")
    if job.status == "cancelled":
        return text_parts(f"{head}That job was cancelled.")
    history = list(meta.get("history") or [])
    last = next((h.get("content") for h in reversed(history) if h.get("role") == "assistant"), "") or job.result or ""
    if job.status == "done":
        parts += text_parts(head + (job.result or last or "Done."))
    elif job.status == "waiting_approval":
        if last and last.strip() != "Done.":
            parts += text_parts(head + last)
        for i, card in enumerate(list(meta.get("pending") or [])[:5]):
            preview = to_whatsapp(str(card.get("preview") or ""))[:700]
            body = f"*Approval needed:* {card.get('title') or 'action'}" + (f"\n\n{preview}" if preview else "")
            parts.append({"type": "buttons", "text": body, "buttons": [[button_id("ap", job.id, i), "Approve"], [button_id("dn", job.id, i), "Deny"]]})
    elif job.status == "waiting_input":
        q = meta.get("question") or {}
        opts = list(q.get("options") or [])
        if last and last.strip() != (q.get("question") or "").strip():
            parts += text_parts(head + last)
        question = to_whatsapp(str(q.get("question") or "Which one?"))
        if 0 < len(opts) <= BUTTON_MAX and all(len(o) <= 20 for o in opts):
            parts.append({"type": "buttons", "text": question, "buttons": [[button_id("op", job.id, i), o] for i, o in enumerate(opts)]})
        else:
            listing = "\n".join(f"{i}. {o}" for i, o in enumerate(opts, 1))
            parts += text_parts(f"{question}\n{listing}\n\nReply with a number or your own answer.")
    elif job.status == "queued" and last:
        parts += text_parts(head + last)  # an answer before it picks up a queued follow-up
    else:
        return []
    for card in (attachments or [])[:5]:
        url = str((card or {}).get("download_url") or (card or {}).get("url") or "")
        if url.startswith("https://"):
            parts.append({"type": "media", "url": url, "kind": _media_kind(card), "filename": str(card.get("filename") or "")[:200]})
    return parts


async def after_run(db: AsyncSession, job: AgentJob, attachments: list | None = None) -> str:
    """Called by the job runner after a turn is folded in. No-op for jobs that did not come from WhatsApp."""
    ch = (job.meta or {}).get("channel") or {}
    if job.parent_id is not None or ch.get("type") != CHANNEL or not enabled():
        return "skip"
    try:
        link = await db.get(ChannelLink, uuid.UUID(str(ch.get("link_id"))))
    except ValueError:
        return "skip"
    if link is None or link.user_id != job.user_id:
        return "skip"
    agent = await db.get(WorkspaceAgent, job.agent_id) if job.agent_id else None
    parts = outcome_parts(job, agent.name if agent else "", attachments)
    _progress_state.pop(str(job.id), None)
    if not parts:
        return "skip"
    preview = (job.result or job.error or "").strip() or "Your agent needs you in Checkme."
    return await deliver(db, link, parts, notify=preview)


async def progress(job: AgentJob, kind: str, text: str) -> bool:
    """Milestone updates while a WhatsApp job runs: plan updates and status changes, rate-limited per job."""
    ch = (job.meta or {}).get("channel") or {}
    if job.parent_id is not None or ch.get("type") != CHANNEL or kind not in {"plan", "status"} or not text:
        return False
    if kind == "status" and (text.startswith(("Queued:", "Picking up", "Follow-up")) or " is working" in text or " is processing" in text):
        return False
    try:
        since = _now() - datetime.fromisoformat(str(ch.get("inbound_at")))
    except (TypeError, ValueError):
        return False
    if since > WINDOW - WINDOW_MARGIN or not enabled():
        return False
    st = _progress_state.setdefault(str(job.id), {"n": 0, "at": 0.0})
    now = time.monotonic()
    if st["n"] >= PROGRESS_MAX or now - st["at"] < PROGRESS_GAP_S:
        return False
    st["n"] += 1
    st["at"] = now
    try:
        await get_provider().send_text(str(ch.get("address")), "⏳ " + to_whatsapp(text)[:300])
    except SendError as exc:
        log.info("whatsapp progress failed: %s", exc)
        return False
    return True
