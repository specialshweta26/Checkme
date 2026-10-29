"""WhatsApp channel against a real Postgres: linking, unknown numbers, routing to agents, the job round trip,
approval buttons, idempotency and the 24h window outbox.

Skipped unless CHECKME_TEST_DATABASE_URL is set (see tests/test_jobs_db.py)."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app.channels import provider, service
from app.channels.provider import Inbound

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")


class FakeProvider:
    name = "whatsapp"

    def __init__(self):
        self.sent: list[tuple] = []

    def enabled(self):
        return True

    def verify_signature(self, raw, header):
        return True

    def parse(self, payload):
        return []

    async def send_text(self, to, text):
        self.sent.append(("text", to, text))

    async def send_buttons(self, to, body, buttons):
        self.sent.append(("buttons", to, body, list(buttons)))

    async def send_media(self, to, url, kind, caption="", filename=""):
        self.sent.append(("media", to, url, kind))

    async def send_template(self, to, name, lang, params):
        self.sent.append(("template", to, name, params))

    async def download_media(self, media_id):
        return b"OggS-voice", "audio/ogg"

    def texts(self, to=None):
        return [s[2] for s in self.sent if s[0] == "text" and (to is None or s[1] == to)]


@pytest.fixture
async def factory():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import _migrate_jobs
    from app.models import Base

    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_jobs(conn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def fake(monkeypatch):
    fp = FakeProvider()
    provider.set_provider(fp)

    async def never(*a, **k):
        return False

    monkeypatch.setattr(service, "_limited", never)
    yield fp
    provider.set_provider(None)


def _phone() -> str:
    return "+91" + str(uuid.uuid4().int)[:10]


def msg(address, body="", kind="text", **kw) -> Inbound:
    return Inbound(message_id=f"wamid.{uuid.uuid4().hex}", address=address, kind=kind, text=body, **kw)


async def _user(factory, agents=("Tony",)):
    from app.agents.service import ensure_default_agent
    from app.models import User, WorkspaceAgent

    async with factory() as db:
        user = User(email=f"{uuid.uuid4().hex[:8]}@x.test", primary_provider="google")
        db.add(user)
        await db.flush()
        chief = await ensure_default_agent(db, user)
        made = {}
        for name in agents:
            a = WorkspaceAgent(user_id=user.id, name=name, title=f"{name} title", kind="custom")
            db.add(a)
            made[name] = a
        await db.commit()
        return user, chief, made


async def _linked(factory, fake, agents=("Tony",)):
    user, chief, made = await _user(factory, agents)
    async with factory() as db:
        out = await service.create_link_code(db, user)
        await db.commit()
    phone = _phone()
    assert await service.handle_inbound(msg(phone, f"link {out['code'].lower()}", name="Shweta"), factory) == "linked"
    return user, chief, made, phone


async def _jobs(factory, user):
    from app.models import AgentJob

    async with factory() as db:
        return list((await db.execute(select(AgentJob).where(AgentJob.user_id == user.id).order_by(AgentJob.created_at))).scalars().all())


async def test_link_code_links_number_and_is_single_use(factory, fake, monkeypatch):
    from app.config import get_settings
    from app.models import ChannelLink

    monkeypatch.setattr(get_settings(), "whatsapp_business_number", "+1 555 000 1111")
    user, _, _ = await _user(factory)
    async with factory() as db:
        out = await service.create_link_code(db, user)
        await db.commit()
    assert out["wa_link"] == f"https://wa.me/15550001111?text=link%20{out['code']}"
    phone = _phone()
    assert await service.handle_inbound(msg(phone, f"link {out['code']}", name="Shweta"), factory) == "linked"
    assert "connected" in fake.texts(phone)[0]
    async with factory() as db:
        link = await service.find_link(db, phone)
        assert link.user_id == user.id and link.display_name == "Shweta" and link.last_inbound_at is not None
        st = await service.status(db, user)
        assert [x["address"] for x in st["links"]] == [phone]
    other = _phone()
    assert await service.handle_inbound(msg(other, f"link {out['code']}"), factory) == "link_invalid"  # used up
    async with factory() as db:
        assert await service.find_link(db, other) is None
        assert (await db.execute(select(ChannelLink).where(ChannelLink.user_id == user.id))).scalars().all()


async def test_expired_code_and_unknown_numbers_get_no_access(factory, fake):
    from app.models import ChannelLinkCode

    user, _, _ = await _user(factory)
    async with factory() as db:
        out = await service.create_link_code(db, user)
        row = await db.get(ChannelLinkCode, out["code"])
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        await db.commit()
    phone = _phone()
    assert await service.handle_inbound(msg(phone, f"link {out['code']}"), factory) == "link_invalid"
    assert await service.handle_inbound(msg(phone, "show me my email"), factory) == "unknown"
    assert fake.texts(phone)[-1] == service.text_parts(service.UNKNOWN)[0]["text"]
    assert await _jobs(factory, user) == []


async def test_routing_default_target_sticky_and_thread(factory, fake):
    from app.models import ChatMessage, ChatSession

    user, chief, made, phone = await _linked(factory, fake, ("Tony", "Coder Bot"))
    assert await service.handle_inbound(msg(phone, "what's on my calendar?"), factory) == "job_new"
    assert await service.handle_inbound(msg(phone, "tony: check my mail"), factory) == "job_new"
    assert await service.handle_inbound(msg(phone, "Note: buy milk"), factory) == "job_followup"  # default agent, not "Note"
    jobs = await _jobs(factory, user)
    assert [j.agent_id for j in jobs] == [chief.id, made["Tony"].id]
    tony = jobs[1]
    assert tony.kind == "chat" and "check my mail" in tony.prompt and "WhatsApp" in tony.prompt
    assert tony.meta["channel"]["address"] == phone and tony.meta["channel"]["show_name"] is True
    assert jobs[0].meta["channel"]["show_name"] is False
    assert "buy milk" in " ".join(jobs[0].meta.get("followups") or [])
    async with factory() as db:
        sess = await db.get(ChatSession, tony.session_id)
        assert sess.state == {"channel": "whatsapp"} and sess.agent_id == made["Tony"].id and sess.user_id == user.id
        rows = (await db.execute(select(ChatMessage).where(ChatMessage.session_id == sess.id).order_by(ChatMessage.id))).scalars().all()
        assert [(m.role, m.meta.get("channel")) for m in rows] == [("user", "whatsapp"), ("assistant", "whatsapp")]
        assert rows[1].meta["job"]["id"] == str(tony.id)  # the app shows the live timeline
    assert "Tony" in fake.texts(phone)[-2] and "Tony" not in fake.texts(phone)[-3]

    assert await service.handle_inbound(msg(phone, "/use coder"), factory) == "use_set"
    assert await service.handle_inbound(msg(phone, "status?"), factory) == "job_new"
    assert (await _jobs(factory, user))[-1].agent_id == made["Coder Bot"].id
    assert await service.handle_inbound(msg(phone, "/use nobody"), factory) == "use_unknown"
    assert await service.handle_inbound(msg(phone, "/use default"), factory) == "use_reset"
    assert await service.handle_inbound(msg(phone, "/agents"), factory) == "agents"
    listing = fake.texts(phone)[-1]
    assert "*Tony* — Tony title" in listing and "(default)" in listing
    assert await service.handle_inbound(msg(phone, "/help"), factory) == "help"


async def test_job_round_trip_with_approval_buttons(factory, fake):
    from app.agent.confirm import sign_confirm
    from app.jobs import worker
    from app.jobs.runner import run_job
    from app.models import AgentJob, ChatMessage

    user, chief, made, phone = await _linked(factory, fake)
    assert await service.handle_inbound(msg(phone, "email bob that I'm late"), factory) == "job_new"
    job_id = (await _jobs(factory, user))[0].id
    await worker.claim(factory, 4, set())
    token = sign_confirm("tool", {"slug": "gmail", "tool": "send", "args": {"to": "bob@x.test"}}, ttl=3600)

    async def agent_impl(db, user_, session_id, request_id, history, query, confirmations, **kw):
        if not confirmations:
            return {"text": "Drafted it.", "pending_confirms": [{"token": token, "title": "Send email to Bob", "preview": "Running late"}]}
        return {"text": "**Sent** to Bob."}

    fake.sent.clear()
    assert await run_job(job_id, factory, agent_impl) == "waiting_approval"
    buttons = [s for s in fake.sent if s[0] == "buttons"]
    assert fake.texts(phone) == ["Drafted it."]
    assert buttons and buttons[0][3] == [(f"wa:ap:{job_id}:0", "Approve"), (f"wa:dn:{job_id}:0", "Deny")]

    # a different user's number cannot press it
    stranger_user, _, _, stranger = await _linked(factory, fake)
    assert await service.handle_inbound(msg(stranger, "Approve", kind="button", button_id=f"wa:ap:{job_id}:0"), factory) == "button_denied"

    assert await service.handle_inbound(msg(phone, "Approve", kind="button", button_id=f"wa:ap:{job_id}:0"), factory) == "job_approved"
    async with factory() as db:
        job = await db.get(AgentJob, job_id)
        assert job.status == "queued" and job.meta["confirmations"]
    assert await service.handle_inbound(msg(phone, "Approve", kind="button", button_id=f"wa:ap:{job_id}:0"), factory) == "approval_stale"

    await worker.claim(factory, 4, set())
    fake.sent.clear()
    assert await run_job(job_id, factory, agent_impl) == "done"
    assert fake.texts(phone) == ["*Sent* to Bob."]
    async with factory() as db:
        job = await db.get(AgentJob, job_id)
        posted = (await db.execute(select(ChatMessage).where(ChatMessage.session_id == job.session_id, ChatMessage.role == "assistant"))).scalars().all()
        assert any(m.content == "**Sent** to Bob." for m in posted)  # also in the app chat

    # the next message continues the conversation with the job's history
    assert await service.handle_inbound(msg(phone, "thanks, and tell Alice too"), factory) == "job_new"
    newest = (await _jobs(factory, user))[-1]
    assert newest.id != job_id and any("Sent" in h["content"] for h in newest.meta["history"])


async def test_question_option_button_and_numbered_reply(factory, fake):
    from app.jobs import worker
    from app.jobs.runner import run_job
    from app.models import AgentJob

    user, chief, made, phone = await _linked(factory, fake)
    await service.handle_inbound(msg(phone, "book a table"), factory)
    job_id = (await _jobs(factory, user))[0].id
    await worker.claim(factory, 4, set())

    async def asks(db, user_, session_id, request_id, history, query, confirmations, **kw):
        return {"text": "Which time?", "question": {"question": "Which time?", "options": ["7pm", "8pm", "9pm", "10pm"]}}

    assert await run_job(job_id, factory, asks) == "waiting_input"
    assert "1. 7pm" in fake.texts(phone)[-1]
    assert await service.handle_inbound(msg(phone, "2"), factory) == "job_answer"
    async with factory() as db:
        job = await db.get(AgentJob, job_id)
        assert job.status == "queued" and "8pm" in job.meta["next_query"]


async def test_webhook_retries_are_claimed_once(factory):
    async with factory() as db:
        mid = f"wamid.{uuid.uuid4().hex}"
        assert await service.claim_message(db, mid) is True
        assert await service.claim_message(db, mid) is False
        await db.commit()


async def test_outside_window_queues_then_flushes(factory, fake):
    from app.jobs import worker
    from app.jobs.runner import run_job
    from app.models import ChannelLink

    user, chief, made, phone = await _linked(factory, fake)
    await service.handle_inbound(msg(phone, "long research task"), factory)
    job_id = (await _jobs(factory, user))[0].id
    async with factory() as db:
        link = await service.find_link(db, phone)
        link.last_inbound_at = datetime.now(timezone.utc) - timedelta(hours=30)
        await db.commit()
    await worker.claim(factory, 4, set())

    async def done(db, user_, session_id, request_id, history, query, confirmations, **kw):
        return {"text": "Here is the report."}

    fake.sent.clear()
    assert await run_job(job_id, factory, done) == "done"
    assert fake.sent == []  # no template configured: held
    async with factory() as db:
        link = await db.get(ChannelLink, (await service.find_link(db, phone)).id)
        assert [p["text"] for p in link.outbox] == ["Here is the report."]
    await service.handle_inbound(msg(phone, "/help"), factory)
    assert fake.texts(phone)[0] == "Here is the report."
    async with factory() as db:
        assert (await service.find_link(db, phone)).outbox == []


async def test_voice_note_is_transcribed(factory, fake, monkeypatch):
    import app.skills.narration as narration

    async def transcribe(data, ext):
        assert data == b"OggS-voice" and ext == "ogg"
        return [{"start": 0, "end": 1, "text": "remind me to call mom"}]

    monkeypatch.setattr(narration, "transcribe", transcribe)
    user, chief, made, phone = await _linked(factory, fake)
    voice = msg(phone, kind="audio", media_id="M1", mime_type="audio/ogg; codecs=opus")
    assert await service.handle_inbound(voice, factory) == "job_new"
    assert "(voice note) remind me to call mom" in (await _jobs(factory, user))[0].prompt


async def test_unlink(factory, fake):
    user, chief, made, phone = await _linked(factory, fake)
    async with factory() as db:
        link = await service.find_link(db, phone)
        other, _, _ = await _user(factory)
        assert not await service.unlink(db, other, link.id)  # someone else's link
        assert await service.unlink(db, user, link.id)
        await db.commit()
    assert await service.handle_inbound(msg(phone, "hello"), factory) == "unknown"


async def test_room_routes_to_group_lead_in_group_thread(factory, fake):
    from app.models import AgentGroup, AgentGroupMember, ChatSession

    user, chief, made, phone = await _linked(factory, fake, ("Tony", "Ana"))
    async with factory() as db:
        group = AgentGroup(user_id=user.id, name="Launch team", owner_id=made["Ana"].id)
        db.add(group)
        await db.flush()
        for a in made.values():
            db.add(AgentGroupMember(group_id=group.id, agent_id=a.id))
        await db.commit()
    assert await service.handle_inbound(msg(phone, "/room launch"), factory) == "room_set"
    assert await service.handle_inbound(msg(phone, "plan the launch"), factory) == "job_new"
    job = (await _jobs(factory, user))[-1]
    assert job.agent_id == made["Ana"].id
    async with factory() as db:
        sess = await db.get(ChatSession, job.session_id)
        assert sess.group_id == group.id and sess.agent_id is None
    assert await service.handle_inbound(msg(phone, "/room off"), factory) == "room_off"
    assert await service.handle_inbound(msg(phone, "/room nope"), factory) == "room_unknown"
