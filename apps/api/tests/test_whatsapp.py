"""WhatsApp channel: signature, webhook verify/receive, parsing, routing commands, buttons, chunking, 24h window.

Pure tests with a fake provider; tests/test_whatsapp_db.py covers linking and the job flow against Postgres."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.channels import formatting, provider, service

SECRET = "app-secret-for-tests"


def _settings(**kw):
    base = dict(
        whatsapp_phone_number_id="111", whatsapp_access_token="tok", whatsapp_app_secret=SECRET,
        whatsapp_verify_token="verify-me", whatsapp_graph_version="v21.0",
    )
    base.update(kw)
    return SimpleNamespace(**base)


class FakeProvider:
    name = "whatsapp"

    def __init__(self, on=True):
        self.on = on
        self.sent: list[tuple] = []

    def enabled(self):
        return self.on

    def verify_signature(self, raw, header):
        return provider.check_signature(SECRET, raw, header)

    def parse(self, payload):
        return provider.MetaWhatsApp(settings=_settings()).parse(payload)

    async def send_text(self, to, text):
        self.sent.append(("text", to, text))
        return "wamid.out"

    async def send_buttons(self, to, body, buttons):
        self.sent.append(("buttons", to, body, list(buttons)))
        return "wamid.out"

    async def send_media(self, to, url, kind, caption="", filename=""):
        self.sent.append(("media", to, url, kind))
        return "wamid.out"

    async def send_template(self, to, name, lang, params):
        self.sent.append(("template", to, name, params))
        return "wamid.out"

    async def download_media(self, media_id):
        return b"data", "image/png"


@pytest.fixture
def fake(monkeypatch):
    fp = FakeProvider()
    provider.set_provider(fp)

    async def never(*a, **k):
        return False

    monkeypatch.setattr(service, "_limited", never)
    yield fp
    provider.set_provider(None)


def _payload(messages, phone_id="111", contacts=None):
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "15550001111", "phone_number_id": phone_id},
            "contacts": contacts or [{"wa_id": "919876543210", "profile": {"name": "Shweta"}}],
            "messages": messages,
        }}]}],
    }


# ---- signature ----


def test_signature_matches_hmac_of_raw_body():
    raw = b'{"a": 1}'
    good = provider.sign(SECRET, raw)
    assert good.startswith("sha256=")
    assert provider.check_signature(SECRET, raw, good)
    assert not provider.check_signature(SECRET, raw + b" ", good)  # any byte change fails
    assert not provider.check_signature(SECRET, raw, "sha256=" + "0" * 64)
    assert not provider.check_signature(SECRET, raw, None)
    assert not provider.check_signature("", raw, good)


def test_disabled_without_config():
    assert not provider.MetaWhatsApp(settings=_settings(whatsapp_app_secret="")).enabled()
    assert not provider.MetaWhatsApp(settings=_settings(whatsapp_verify_token="")).enabled()
    assert provider.MetaWhatsApp(settings=_settings()).enabled()


# ---- parsing ----


def test_parse_text_button_media_and_ignores_other_numbers():
    meta = provider.MetaWhatsApp(settings=_settings())
    msgs = meta.parse(_payload([
        {"from": "919876543210", "id": "wamid.1", "timestamp": "1730000000", "type": "text", "text": {"body": "hi"}},
        {"from": "919876543210", "id": "wamid.2", "type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": "wa:ap:x:0", "title": "Approve"}}},
        {"from": "919876543210", "id": "wamid.3", "type": "audio", "audio": {"id": "M1", "mime_type": "audio/ogg; codecs=opus", "voice": True}},
        {"from": "919876543210", "id": "wamid.4", "type": "document", "document": {"id": "M2", "mime_type": "application/pdf", "filename": "a.pdf", "caption": "read"}},
    ]))
    assert [m.kind for m in msgs] == ["text", "button", "audio", "document"]
    assert msgs[0].address == "+919876543210" and msgs[0].name == "Shweta" and msgs[0].text == "hi" and msgs[0].timestamp == 1730000000
    assert msgs[1].button_id == "wa:ap:x:0"
    assert msgs[2].media_id == "M1" and msgs[2].extra["voice"]
    assert msgs[3].filename == "a.pdf" and msgs[3].text == "read"
    assert meta.parse(_payload([{"from": "1555", "id": "x", "type": "text", "text": {"body": "x"}}], phone_id="999")) == []
    assert meta.parse({"object": "page"}) == []
    # status callbacks (delivered/read) carry no messages
    assert meta.parse({"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {"statuses": [{"id": "s"}]}}]}]}) == []


def test_e164():
    assert provider.e164("+91 98765-43210") == "+919876543210"
    assert provider.e164("15551234567") == "+15551234567"
    assert provider.e164("123") == ""


# ---- formatting and chunking ----


def test_markdown_to_whatsapp():
    out = formatting.to_whatsapp("## Plan\n**Bold** and *it* and ~~old~~, see [PR](https://github.com/a/b/pull/1)\n- one\n* two\n`x**y**`")
    assert out.splitlines()[0] == "*Plan*"
    assert "*Bold* and _it_ and ~old~" in out
    assert "PR (https://github.com/a/b/pull/1)" in out
    assert "• one" in out and "• two" in out
    assert "`x**y**`" in out  # code is left alone


def test_code_fence_kept():
    out = formatting.to_whatsapp("Run:\n```bash\nmake **all**\n```")
    assert "```make **all**```" in out


def test_chunk_limits_and_counters():
    assert formatting.chunk("short") == ["short"]
    assert formatting.chunk("") == []
    parts = formatting.chunk("paragraph text here. " * 600)
    assert len(parts) > 1 and all(len(p) <= 4096 for p in parts)
    assert parts[0].endswith(f"(1/{len(parts)})") and parts[-1].endswith(f"({len(parts)}/{len(parts)})")


def test_chunk_closes_and_reopens_code_fences():
    parts = formatting.chunk("```\n" + "print('x')\n" * 900 + "```")
    assert len(parts) >= 2
    for p in parts:
        body = p.rsplit("\n(", 1)[0]
        assert body.count("```") % 2 == 0
        assert len(p) <= 4096


def test_chunk_hard_cuts_one_long_word():
    parts = formatting.chunk("x" * 9000)
    assert all(len(p) <= 4096 for p in parts) and len(parts) == 3


# ---- commands and routing ----


@pytest.mark.parametrize(
    "text,kind,arg,target,body",
    [
        ("link ab3k9z", "link", "AB3K9Z", "", ""),
        ("/help", "help", "", "", ""),
        ("/agents", "agents", "", "", ""),
        ("/use Tony", "use", "Tony", "", ""),
        ("/use", "use", "", "", ""),
        ("/room Launch team", "room", "Launch team", "", ""),
        ("/room off", "room", "off", "", ""),
        ("/new", "new", "", "", ""),
        ("/unlink", "unlink", "", "", ""),
        ("tony: check my mail", "message", "", "tony", "check my mail"),
        ("@tony check my mail", "message", "", "tony", "check my mail"),
        ("@Tony, check", "message", "", "Tony", "check"),
        ("what's up", "message", "", "", "what's up"),
    ],
)
def test_parse_commands(text, kind, arg, target, body):
    cmd = service.parse(text)
    assert (cmd.kind, cmd.arg, cmd.target) == (kind, arg, target)
    if body:
        assert cmd.body == body


def test_fuzzy_agent_match():
    agents = [SimpleNamespace(name="Tony", archived=False), SimpleNamespace(name="Coder", archived=False),
              SimpleNamespace(name="Research Bot", archived=False), SimpleNamespace(name="Old", archived=True)]
    assert service.match_name(agents, "tony").name == "Tony"
    assert service.match_name(agents, "@TONY").name == "Tony"
    assert service.match_name(agents, "tonny").name == "Tony"  # close spelling
    assert service.match_name(agents, "research").name == "Research Bot"
    assert service.match_name(agents, "old") is None  # archived
    assert service.match_name(agents, "note") is None


# ---- 24h window and delivery ----


def test_window():
    now = datetime.now(timezone.utc)
    assert service.in_window(SimpleNamespace(last_inbound_at=now - timedelta(hours=2)), now)
    assert not service.in_window(SimpleNamespace(last_inbound_at=now - timedelta(hours=24, minutes=1)), now)
    assert not service.in_window(SimpleNamespace(last_inbound_at=None), now)


async def test_deliver_inside_window_sends(fake):
    link = SimpleNamespace(address="+919876543210", last_inbound_at=datetime.now(timezone.utc), outbox=[])
    assert await service.deliver(None, link, service.text_parts("**hi**")) == "sent"
    assert fake.sent == [("text", "+919876543210", "*hi*")]


async def test_deliver_outside_window_queues_and_uses_template(fake, monkeypatch):
    from app.config import get_settings

    link = SimpleNamespace(address="+919876543210", last_inbound_at=datetime.now(timezone.utc) - timedelta(days=2), outbox=[])
    monkeypatch.setattr(get_settings(), "whatsapp_template_notify", "")
    assert await service.deliver(None, link, service.text_parts("result one")) == "queued"
    assert fake.sent == [] and link.outbox == [{"type": "text", "text": "result one"}]
    monkeypatch.setattr(get_settings(), "whatsapp_template_notify", "checkme_update")
    assert await service.deliver(None, link, service.text_parts("result two"), notify="Your PR is ready") == "queued"
    assert fake.sent == [("template", "+919876543210", "checkme_update", ["Your PR is ready"])]
    assert len(link.outbox) == 2
    # the user writes again: the held messages go out in order
    assert await service.flush_outbox(link) == 2
    assert [s[2] for s in fake.sent[1:]] == ["result one", "result two"] and link.outbox == []


# ---- buttons ----


def test_button_ids_roundtrip():
    jid = uuid.uuid4()
    assert service.parse_button(service.button_id("ap", jid, 1)) == ("ap", jid, 1)
    assert service.parse_button("wa:zz:%s:1" % jid) is None
    assert service.parse_button("nonsense") is None


def _job(status, **meta):
    return SimpleNamespace(id=uuid.uuid4(), status=status, result=meta.pop("result", ""), error=meta.pop("error", ""), meta=meta, parent_id=None)


def test_approval_card_becomes_buttons():
    job = _job("waiting_approval", pending=[{"token": "t", "title": "Send email to Bob", "preview": "**Hi Bob**"}],
               history=[{"role": "assistant", "content": "Drafted it."}], channel={"type": "whatsapp"})
    parts = service.outcome_parts(job, "Tony")
    assert parts[0] == {"type": "text", "text": "Drafted it."}
    card = parts[1]
    assert card["type"] == "buttons" and "Send email to Bob" in card["text"] and "*Hi Bob*" in card["text"]
    assert card["buttons"] == [[f"wa:ap:{job.id}:0", "Approve"], [f"wa:dn:{job.id}:0", "Deny"]]


def test_question_options_buttons_or_numbered_list():
    job = _job("waiting_input", question={"question": "Merge it?", "options": ["Merge", "Not now"]}, result="PR opened", channel={})
    parts = service.outcome_parts(job)
    assert parts[-1]["type"] == "buttons" and parts[-1]["buttons"][1] == [f"wa:op:{job.id}:1", "Not now"]
    many = _job("waiting_input", question={"question": "Pick", "options": ["a", "b", "c", "d"]}, channel={})
    text = service.outcome_parts(many)[-1]["text"]
    assert "1. a" in text and "4. d" in text


def test_done_sends_long_answer_in_chunks_with_media_and_name():
    job = _job("done", result="word " * 2000, channel={"type": "whatsapp", "show_name": True})
    parts = service.outcome_parts(job, "Tony", [{"download_url": "https://x/api/files?t=1", "content_type": "image/png", "filename": "shot.png"}])
    texts = [p for p in parts if p["type"] == "text"]
    assert len(texts) == 3 and texts[0]["text"].startswith("*Tony*\n") and all(len(p["text"]) <= 4096 for p in texts)
    assert parts[-1] == {"type": "media", "url": "https://x/api/files?t=1", "kind": "image", "filename": "shot.png"}
    failed = service.outcome_parts(_job("failed", error="boom", channel={}))
    assert "boom" in failed[0]["text"]


async def test_progress_is_rate_limited(fake):
    job = _job("running", channel={"type": "whatsapp", "address": "+919876543210", "inbound_at": datetime.now(timezone.utc).isoformat()})
    assert await service.progress(job, "plan", "1. Read mail 2. Draft reply")
    assert not await service.progress(job, "plan", "updated plan")  # too soon
    assert not await service.progress(job, "step", "clicking")  # not a milestone
    service._progress_state[str(job.id)]["at"] -= 1000
    assert await service.progress(job, "status", "Approved: send")
    assert len(fake.sent) == 2 and fake.sent[0][2].startswith("⏳")
    other = _job("running", channel={"type": "whatsapp", "address": "+1", "inbound_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()})
    assert not await service.progress(other, "plan", "x")  # window closed


# ---- webhook routes ----


@pytest.fixture
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.channels.routes import router
    from app.db import get_db

    provider.set_provider(provider.MetaWhatsApp(settings=_settings()))
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "whatsapp_verify_token", "verify-me")

    class DB:
        commits = 0

        async def commit(self):
            DB.commits += 1

    async def fake_db():
        yield DB()

    seen: set = set()
    handled: list = []

    async def claim(db, mid):
        if mid in seen:
            return False
        seen.add(mid)
        return True

    async def handle(msg, session_factory=None):
        handled.append(msg)
        return "ok"

    monkeypatch.setattr(service, "claim_message", claim)
    monkeypatch.setattr(service, "handle_inbound", handle)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = fake_db
    c = TestClient(app)
    c.handled = handled
    yield c
    provider.set_provider(None)


def test_webhook_verify(client):
    ok = client.get("/api/channels/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "12345"})
    assert ok.status_code == 200 and ok.text == "12345"
    bad = client.get("/api/channels/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"})
    assert bad.status_code == 403


def test_webhook_rejects_bad_signature(client):
    raw = json.dumps(_payload([{"from": "919876543210", "id": "wamid.9", "type": "text", "text": {"body": "hi"}}])).encode()
    r = client.post("/api/channels/whatsapp/webhook", content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 401
    r = client.post("/api/channels/whatsapp/webhook", content=raw, headers={"x-hub-signature-256": provider.sign("wrong", raw)})
    assert r.status_code == 401
    assert client.handled == []


def test_webhook_accepts_signed_raw_body_once(client):
    # Signed over the exact bytes Meta sends (spacing included), so the proxy must pass the raw body through.
    raw = json.dumps(_payload([{"from": "919876543210", "id": "wamid.10", "type": "text", "text": {"body": "hi"}}]), indent=1).encode()
    headers = {"x-hub-signature-256": provider.sign(SECRET, raw), "content-type": "application/json"}
    r = client.post("/api/channels/whatsapp/webhook", content=raw, headers=headers)
    assert r.status_code == 200 and r.json()["accepted"] == 1
    again = client.post("/api/channels/whatsapp/webhook", content=raw, headers=headers)  # Meta retry
    assert again.status_code == 200 and again.json()["accepted"] == 0
    assert [m.message_id for m in client.handled] == ["wamid.10"]


def test_webhook_404_when_disabled(client):
    provider.set_provider(FakeProvider(on=False))
    assert client.get("/api/channels/whatsapp/webhook").status_code == 404
    assert client.post("/api/channels/whatsapp/webhook", content=b"{}").status_code == 404
