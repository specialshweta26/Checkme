"""Mail Lumio sends for sign-up and password reset goes through Resend or SendGrid (docs/AUTH.md "Mail")."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.auth import mailer, passwords
from app.config import get_settings


@pytest.fixture
def calls(monkeypatch):
    out: list[dict] = []

    class Client:
        async def post(self, url, headers=None, json=None):
            out.append({"url": url, "headers": headers, "json": json})
            return SimpleNamespace(status_code=202, text="")

    import connector_core.http as http

    monkeypatch.setattr(http, "shared_client", lambda: Client())
    s = get_settings()
    for k in ("resend_api_key", "sendgrid_api_key", "smtp_host"):
        monkeypatch.setattr(s, k, "")
    monkeypatch.setattr(s, "mail_from", "Lumio <no-reply@lumio.example>")
    return out


async def test_resend(calls, monkeypatch):
    monkeypatch.setattr(get_settings(), "resend_api_key", "re_test")
    assert mailer.provider() == "resend"
    text, page = mailer.render("Reset", ["Line"], "Go", "https://x.example/reset?token=t")
    assert await mailer.send("a@b.com", "Reset your Lumio password", text, page)
    c = calls[0]
    assert c["url"] == "https://api.resend.com/emails" and c["headers"]["Authorization"] == "Bearer re_test"
    assert c["json"]["from"] == "Lumio <no-reply@lumio.example>" and c["json"]["to"] == ["a@b.com"]
    assert "token=t" in c["json"]["text"] and "token=t" in c["json"]["html"]


async def test_sendgrid_without_click_tracking(calls, monkeypatch):
    monkeypatch.setattr(get_settings(), "sendgrid_api_key", "SG.test")
    assert mailer.provider() == "sendgrid"
    assert await mailer.send("a@b.com", "Confirm", "t", "<p>h</p>")
    c = calls[0]
    assert c["url"] == "https://api.sendgrid.com/v3/mail/send" and c["headers"]["Authorization"] == "Bearer SG.test"
    assert c["json"]["personalizations"] == [{"to": [{"email": "a@b.com"}]}]
    assert c["json"]["from"] == {"email": "no-reply@lumio.example", "name": "Lumio"}
    assert c["json"]["tracking_settings"]["click_tracking"]["enable"] is False  # links keep working as written


async def test_resend_wins_when_both_are_set(calls, monkeypatch):
    monkeypatch.setattr(get_settings(), "resend_api_key", "re_test")
    monkeypatch.setattr(get_settings(), "sendgrid_api_key", "SG.test")
    assert mailer.provider() == "resend"


def test_not_configured_in_production(monkeypatch):
    s = get_settings()
    for k in ("resend_api_key", "sendgrid_api_key", "smtp_host"):
        monkeypatch.setattr(s, k, "")
    monkeypatch.setattr(s, "public_web_url", "https://lumio.example.com")
    assert mailer.provider() == "" and not mailer.configured()


def test_password_hash_round_trip_and_rules():
    h = passwords.hash_password_sync("a long pass phrase")
    assert h.startswith("scrypt$") and "a long pass phrase" not in h
    assert passwords.verify_password_sync("a long pass phrase", h)
    assert not passwords.verify_password_sync("a long pass phrasE", h)
    assert not passwords.verify_password_sync("x", "garbage")
    assert passwords.problem("asha.rao.2026!", "asha.rao@x.com")  # contains the email name
    assert passwords.problem("a long pass phrase", "a@x.com") == ""
    assert passwords.fingerprint(h) != passwords.fingerprint(passwords.hash_password_sync("a long pass phrase"))
