"""WhatsApp over Twilio: signature, webhook, inbound parsing, outbound payloads, splitting, provider choice."""

from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import urlencode

import httpx
import pytest

from app.channels import provider, service

TOKEN = "twilio-auth-token-for-tests"
BASE = "https://checkme-web.example.com"
URL = BASE + "/api/channels/whatsapp/twilio"


def _settings(**kw):
    base = dict(
        twilio_account_sid="AC" + "0" * 32, twilio_auth_token=TOKEN, twilio_api_key_sid="SK" + "1" * 32,
        twilio_api_key_secret="key-secret", twilio_whatsapp_number="whatsapp:+14155238886",
        twilio_content_sid_approval="", twilio_content_sid_notify="", twilio_status_callback=False,
        whatsapp_public_base_url=BASE, public_web_url="http://localhost:3000", whatsapp_provider="",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _form(**kw):
    f = {"MessageSid": "SM123", "AccountSid": "AC" + "0" * 32, "From": "whatsapp:+919876543210", "To": "whatsapp:+14155238886",
         "Body": "hello", "NumMedia": "0", "ProfileName": "Shweta"}
    f.update(kw)
    return f


# ---- signature ----


def test_twilio_signature_known_vector():
    # Same value as twilio.request_validator.RequestValidator("12345").compute_signature(url, params).
    url = "https://example.com/myapp.php?foo=1&bar=2"
    params = {"CallSid": "CA1234567890ABCDE", "Caller": "+12349013030", "Digits": "1234", "From": "+12349013030", "To": "+18005551212"}
    assert provider.twilio_signature("12345", url, params) == "vNe7KK2kJwCsxc9K3OLkkKB3qqI="


def test_twilio_signature_checks():
    f = _form()
    good = provider.twilio_signature(TOKEN, URL, f)
    assert provider.check_twilio_signature(TOKEN, URL, f, good)
    assert provider.check_twilio_signature(TOKEN, URL, list(f.items()), good)  # order of fields does not matter
    assert not provider.check_twilio_signature(TOKEN, URL + "x", f, good)  # URL is part of the signature
    assert not provider.check_twilio_signature(TOKEN, URL, {**f, "Body": "hellO"}, good)
    assert not provider.check_twilio_signature(TOKEN, URL, f, None)
    assert not provider.check_twilio_signature("", URL, f, good)


# ---- config ----


def test_enabled_and_choice():
    assert provider.TwilioWhatsApp(settings=_settings()).enabled()
    assert not provider.TwilioWhatsApp(settings=_settings(twilio_auth_token="")).enabled()
    assert not provider.TwilioWhatsApp(settings=_settings(twilio_whatsapp_number="")).enabled()
    assert provider.choose(_settings()) == "twilio"
    assert provider.choose(_settings(twilio_account_sid="")) == "meta"
    assert provider.choose(_settings(whatsapp_provider="meta")) == "meta"
    assert provider.choose(_settings(twilio_account_sid="", whatsapp_provider="Twilio")) == "twilio"


def test_numbers_normalised():
    assert provider.wa_address("whatsapp:+14155238886") == "whatsapp:+14155238886"
    assert provider.wa_address("+1 415 523 8886") == "whatsapp:+14155238886"
    assert provider.wa_address("") == ""
    p = provider.TwilioWhatsApp(settings=_settings(twilio_whatsapp_number="+14155238886"))
    assert p.business_number() == "+14155238886"
    assert p.webhook_url("/api/channels/whatsapp/twilio") == URL
    p2 = provider.TwilioWhatsApp(settings=_settings(whatsapp_public_base_url="", public_web_url="https://web.example/"))
    assert p2.webhook_url() == "https://web.example/api/channels/whatsapp/twilio"


# ---- inbound ----


def test_parse_text_button_media():
    p = provider.TwilioWhatsApp(settings=_settings())
    [m] = p.parse(_form())
    assert (m.kind, m.address, m.text, m.message_id, m.name) == ("text", "+919876543210", "hello", "SM123", "Shweta")
    [b] = p.parse(_form(ButtonPayload="wa:ap:" + "0" * 36 + ":0", ButtonText="Approve", Body="Approve"))
    assert b.kind == "button" and b.button_id.startswith("wa:ap:") and b.text == "Approve"
    media = "https://api.twilio.com/2010-04-01/Accounts/AC/Messages/MM/Media/ME1"
    [v] = p.parse(_form(Body="", NumMedia="1", MediaUrl0=media, MediaContentType0="audio/ogg"))
    assert v.kind == "audio" and v.media_id == media and v.extra["voice"]
    [i] = p.parse(_form(Body="look", NumMedia="1", MediaUrl0=media, MediaContentType0="image/jpeg"))
    assert i.kind == "image" and i.text == "look"
    [d] = p.parse(_form(Body="", NumMedia="1", MediaUrl0=media, MediaContentType0="application/pdf"))
    assert d.kind == "document"


def test_parse_ignores_non_whatsapp_and_other_senders():
    p = provider.TwilioWhatsApp(settings=_settings())
    assert p.parse(_form(From="+919876543210")) == []  # SMS, not WhatsApp
    assert p.parse(_form(To="whatsapp:+15550001111")) == []
    assert p.parse(_form(MessageSid="")) == []


# ---- outbound ----


class Recorder:
    def __init__(self):
        self.calls: list = []

    async def post(self, url, data=None, auth=None):
        self.calls.append((url, data, auth))
        return httpx.Response(201, json={"sid": f"SM{len(self.calls)}"})


async def test_send_text_payload_and_auth():
    rec = Recorder()
    p = provider.TwilioWhatsApp(settings=_settings(), client=rec)
    sid = await p.send_text("+919876543210", "hi there")
    assert sid == "SM1"
    url, data, auth = rec.calls[0]
    assert url.endswith("/Accounts/AC" + "0" * 32 + "/Messages.json")
    assert data == {"From": "whatsapp:+14155238886", "To": "whatsapp:+919876543210", "Body": "hi there"}
    assert auth == ("SK" + "1" * 32, "key-secret")
    # No API key: account SID + auth token.
    rec2 = Recorder()
    p2 = provider.TwilioWhatsApp(settings=_settings(twilio_api_key_sid="", twilio_api_key_secret=""), client=rec2)
    await p2.send_text("+919876543210", "hi")
    assert rec2.calls[0][2] == ("AC" + "0" * 32, TOKEN)


async def test_send_text_splits_at_1600():
    rec = Recorder()
    p = provider.TwilioWhatsApp(settings=_settings(), client=rec)
    text = "\n\n".join(f"Paragraph {i} " + "word " * 80 for i in range(12))  # ~5000 chars
    await p.send_text("+919876543210", text)
    bodies = [c[1]["Body"] for c in rec.calls]
    assert len(bodies) >= 4 and all(len(b) <= 1600 for b in bodies)
    assert bodies[0].endswith(f"(1/{len(bodies)})")


async def test_buttons_fallback_text_and_template():
    rec = Recorder()
    p = provider.TwilioWhatsApp(settings=_settings(), client=rec)
    jid = "0" * 8 + "-0000-0000-0000-" + "0" * 12
    await p.send_buttons("+919876543210", "Approval needed: send email", [(f"wa:ap:{jid}:0", "Approve"), (f"wa:dn:{jid}:0", "Deny")])
    assert "Reply *YES* to approve or *NO* to deny." in rec.calls[0][1]["Body"]
    await p.send_buttons("+919876543210", "Which one?", [(f"wa:op:{jid}:0", "Goa"), (f"wa:op:{jid}:1", "Pune")])
    assert "1. Goa\n2. Pune" in rec.calls[1][1]["Body"]
    rec2 = Recorder()
    p2 = provider.TwilioWhatsApp(settings=_settings(twilio_content_sid_approval="HXapprove"), client=rec2)
    await p2.send_buttons("+919876543210", "Approval needed", [(f"wa:ap:{jid}:0", "Approve"), (f"wa:dn:{jid}:0", "Deny")])
    data = rec2.calls[0][1]
    assert data["ContentSid"] == "HXapprove" and "Body" not in data
    assert json.loads(data["ContentVariables"]) == {"1": "Approval needed", "2": f"wa:ap:{jid}:0", "3": f"wa:dn:{jid}:0"}


async def test_media_template_and_status_callback():
    rec = Recorder()
    p = provider.TwilioWhatsApp(settings=_settings(twilio_status_callback=True), client=rec)
    await p.send_media("+919876543210", "https://files.example/report.pdf", "document", "", "report.pdf")
    data = rec.calls[0][1]
    assert data["MediaUrl"] == "https://files.example/report.pdf" and data["Body"] == "report.pdf"
    assert data["StatusCallback"] == URL + "/status"
    await p.send_template("+919876543210", "HXnotify", "en", ["Your report is ready"])
    data = rec.calls[1][1]
    assert data["ContentSid"] == "HXnotify" and json.loads(data["ContentVariables"]) == {"1": "Your report is ready"}


async def test_download_rejects_foreign_urls():
    p = provider.TwilioWhatsApp(settings=_settings())
    with pytest.raises(provider.SendError):
        await p.download_media("https://evil.example/x")


def test_service_uses_twilio_number_and_limit():
    provider.set_provider(provider.TwilioWhatsApp(settings=_settings()))
    try:
        assert service.business_number() == "+14155238886"
        assert service.wa_link("ABC123") == "https://wa.me/14155238886?text=link%20ABC123"
        parts = service.text_parts("x " * 2000)
        assert len(parts) == 3 and all(len(p["text"]) <= 1600 for p in parts)
    finally:
        provider.set_provider(None)


# ---- webhook route ----


@pytest.fixture
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.channels.routes import router
    from app.db import get_db

    provider.set_provider(provider.TwilioWhatsApp(settings=_settings()))

    class DB:
        async def commit(self):
            pass

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


def _post(client, form, sig=None, url=URL):
    body = urlencode(form)
    headers = {"content-type": "application/x-www-form-urlencoded"}
    if sig is not False:
        headers["x-twilio-signature"] = sig or provider.twilio_signature(TOKEN, url, form)
    return client.post("/api/channels/whatsapp/twilio", content=body, headers=headers)


def test_twilio_webhook_accepts_signed_once(client):
    form = _form(MessageSid="SM900")
    r = _post(client, form)
    assert r.status_code == 200 and "<Response></Response>" in r.text and r.headers["content-type"].startswith("application/xml")
    again = _post(client, form)  # Twilio retry
    assert again.status_code == 200
    assert [m.message_id for m in client.handled] == ["SM900"]


def test_twilio_webhook_rejects_bad_signature(client):
    form = _form(MessageSid="SM901")
    assert _post(client, form, sig=False).status_code == 403
    assert _post(client, form, sig="bogus").status_code == 403
    # Signed for the internal URL instead of the public one: rejected.
    assert _post(client, form, url="http://testserver/api/channels/whatsapp/twilio").status_code == 403
    assert client.handled == []


def test_twilio_status_callback(client):
    form = {"MessageSid": "SM5", "MessageStatus": "failed", "ErrorCode": "63016"}
    sig = provider.twilio_signature(TOKEN, URL + "/status", form)
    r = client.post("/api/channels/whatsapp/twilio/status", content=urlencode(form),
                    headers={"content-type": "application/x-www-form-urlencoded", "x-twilio-signature": sig})
    assert r.status_code == 204


def test_routes_follow_active_provider(client):
    # Twilio active: the Meta webhook is off.
    assert client.post("/api/channels/whatsapp/webhook", content=b"{}").status_code == 404
    provider.set_provider(provider.MetaWhatsApp(settings=SimpleNamespace(
        whatsapp_phone_number_id="1", whatsapp_access_token="t", whatsapp_app_secret="s", whatsapp_verify_token="v")))
    assert _post(client, _form()).status_code == 404
