"""Mail Lumio itself sends: sign-up verification, password reset, password changed (docs/AUTH.md).

One provider, picked by which key is set: Resend (RESEND_API_KEY), SendGrid (SENDGRID_API_KEY), or SMTP (SMTP_HOST).
With none, on a local server the message is written to the API log so sign-up can still be tried; in production
``configured()`` is False and email sign-up is hidden.
"""

from __future__ import annotations

import asyncio
import html
import logging
import smtplib
from email.message import EmailMessage
from email.utils import parseaddr

from app.config import get_settings

log = logging.getLogger(__name__)


def provider() -> str:
    s = get_settings()
    if s.resend_api_key:
        return "resend"
    if s.sendgrid_api_key:
        return "sendgrid"
    if s.smtp_host:
        return "smtp"
    return "log" if s.dev_login_enabled else ""


def configured() -> bool:
    return provider() != ""


def _sender() -> tuple[str, str]:
    s = get_settings()
    name, addr = parseaddr(s.mail_from or "")
    if not addr:
        addr = s.smtp_user if "@" in (s.smtp_user or "") else "no-reply@lumio.local"
    return name or "Lumio", addr


def render(title: str, lines: list[str], button: str = "", link: str = "") -> tuple[str, str]:
    """(plain text, html) for a short message with an optional button."""
    text = "\n\n".join([title, *lines] + ([f"{button}: {link}"] if link else []))
    body = "".join(f'<p style="margin:0 0 14px;line-height:1.5">{html.escape(x)}</p>' for x in lines)
    btn = (
        f'<p style="margin:22px 0"><a href="{html.escape(link)}" style="background:#1f2937;color:#fff;padding:11px 18px;'
        f'border-radius:8px;text-decoration:none;font-weight:600">{html.escape(button)}</a></p>'
        f'<p style="font-size:12px;color:#6b7280;word-break:break-all">Or open this link: {html.escape(link)}</p>'
        if link
        else ""
    )
    page = (
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:520px;margin:0 auto;'
        f'padding:24px;color:#111827"><h2 style="margin:0 0 16px;font-size:20px">{html.escape(title)}</h2>{body}{btn}'
        '<p style="font-size:12px;color:#6b7280;margin-top:28px">Lumio</p></div>'
    )
    return text, page


async def send(to: str, subject: str, text: str, page: str) -> bool:
    """Send one message. Never raises: False when it couldn't be sent (logged without the link)."""
    kind = provider()
    name, addr = _sender()
    s = get_settings()
    try:
        if kind == "resend":
            from connector_core.http import shared_client

            resp = await shared_client().post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {s.resend_api_key}"},
                json={"from": f"{name} <{addr}>", "to": [to], "subject": subject, "text": text, "html": page},
            )
            ok = resp.status_code < 300
        elif kind == "sendgrid":
            from connector_core.http import shared_client

            resp = await shared_client().post(
                "https://api.sendgrid.com/v3/mail/send",
                headers={"Authorization": f"Bearer {s.sendgrid_api_key}"},
                json={
                    "personalizations": [{"to": [{"email": to}]}],
                    "from": {"email": addr, "name": name},
                    "subject": subject,
                    "content": [{"type": "text/plain", "value": text}, {"type": "text/html", "value": page}],
                    "tracking_settings": {"click_tracking": {"enable": False, "enable_text": False}},
                },
            )
            ok = resp.status_code < 300
        elif kind == "smtp":
            await asyncio.to_thread(_smtp, to, subject, text, page, name, addr)
            ok = True
            resp = None
        elif kind == "log":
            log.warning("Mail not configured (local server): to=%s subject=%s\n%s", to, subject, text)
            return True
        else:
            log.warning("Mail not configured: %r to %s was not sent", subject, to)
            return False
    except Exception as exc:  # noqa: BLE001 - a mail outage must not break sign-in
        log.warning("mail via %s failed: %s", kind, type(exc).__name__)
        return False
    if not ok:
        log.warning("mail via %s refused: %s %s", kind, resp.status_code, resp.text[:300])
    return ok


def _smtp(to: str, subject: str, text: str, page: str, name: str, addr: str) -> None:
    s = get_settings()
    msg = EmailMessage()
    msg["From"] = f"{name} <{addr}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(page, subtype="html")
    if s.smtp_port == 465:
        server = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=20)
    else:
        server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20)
        server.starttls()
    with server:
        if s.smtp_user:
            server.login(s.smtp_user, s.smtp_password)
        server.send_message(msg)
