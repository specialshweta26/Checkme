"""Channel providers: one interface, two WhatsApp backends.

- TwilioWhatsApp: Twilio's Messages API with a Twilio WhatsApp sender (form-encoded webhooks, X-Twilio-Signature).
- MetaWhatsApp: the WhatsApp Business Cloud API (graph.facebook.com) directly (JSON webhooks, X-Hub-Signature-256).

WHATSAPP_PROVIDER picks one (twilio | meta); empty means twilio when TWILIO_ACCOUNT_SID is set, else meta.
Both serve one shared business number for every user. Nothing here logs a token, a secret or a message body."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

BUTTON_TITLE_MAX = 20
BUTTON_MAX = 3
INTERACTIVE_BODY_MAX = 1024
TEXT_MAX = 4096


@dataclass
class Inbound:
    message_id: str
    address: str  # E.164 with "+"
    kind: str  # text | button | image | document | audio | other
    text: str = ""
    name: str = ""
    button_id: str = ""
    media_id: str = ""
    mime_type: str = ""
    filename: str = ""
    timestamp: int = 0
    raw_type: str = ""
    extra: dict = field(default_factory=dict)


class SendError(Exception):
    pass


class ChannelProvider(Protocol):
    name: str

    def enabled(self) -> bool: ...

    def verify_signature(self, raw: bytes, header: str | None) -> bool: ...

    def parse(self, payload: dict) -> list[Inbound]: ...

    async def send_text(self, to: str, text: str) -> str: ...

    async def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]]) -> str: ...

    async def send_media(self, to: str, url: str, kind: str, caption: str = "", filename: str = "") -> str: ...

    async def send_template(self, to: str, name: str, lang: str, params: list[str]) -> str: ...

    async def download_media(self, media_id: str) -> tuple[bytes, str]: ...


def e164(raw: str) -> str:
    """'+91 98765-43210' / '919876543210' -> '+919876543210' ('' when it is not a phone number)."""
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if not 8 <= len(digits) <= 15:
        return ""
    return "+" + digits


def sign(secret: str, raw: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def check_signature(secret: str, raw: bytes, header: str | None) -> bool:
    """X-Hub-Signature-256 is 'sha256=<hex HMAC of the raw body with the app secret>'."""
    if not secret or not header:
        return False
    return hmac.compare_digest(sign(secret, raw), header.strip())


def button_title(text: str) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= BUTTON_TITLE_MAX else text[: BUTTON_TITLE_MAX - 1] + "…"


class MetaWhatsApp:
    name = "whatsapp"
    kind = "meta"
    text_max = TEXT_MAX

    def __init__(self, settings=None, client: httpx.AsyncClient | None = None):
        self.s = settings or get_settings()
        self._client = client

    # ---- config ----
    def enabled(self) -> bool:
        s = self.s
        return bool(s.whatsapp_phone_number_id and s.whatsapp_access_token and s.whatsapp_app_secret and s.whatsapp_verify_token)

    def business_number(self) -> str:
        return e164(self.s.whatsapp_business_number)

    def notify_template(self) -> str:
        return str(self.s.whatsapp_template_notify or "").strip()

    @property
    def base(self) -> str:
        return f"https://graph.facebook.com/{self.s.whatsapp_graph_version or 'v21.0'}"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.s.whatsapp_access_token}"}

    # ---- inbound ----
    def verify_signature(self, raw: bytes, header: str | None) -> bool:
        return check_signature(self.s.whatsapp_app_secret, raw, header)

    def parse(self, payload: dict) -> list[Inbound]:
        out: list[Inbound] = []
        if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
            return out
        for entry in payload.get("entry") or []:
            for change in (entry or {}).get("changes") or []:
                value = (change or {}).get("value") or {}
                meta = value.get("metadata") or {}
                ours = str(self.s.whatsapp_phone_number_id or "")
                if ours and str(meta.get("phone_number_id") or "") not in {"", ours}:
                    continue  # another number on the same app
                names = {
                    str(c.get("wa_id") or ""): str(((c.get("profile") or {}).get("name")) or "")
                    for c in value.get("contacts") or []
                    if isinstance(c, dict)
                }
                for m in value.get("messages") or []:
                    item = self._message(m, names)
                    if item is not None:
                        out.append(item)
        return out

    @staticmethod
    def _message(m: dict, names: dict) -> Inbound | None:
        if not isinstance(m, dict) or not m.get("id"):
            return None
        sender = str(m.get("from") or "")
        address = e164(sender)
        if not address:
            return None
        typ = str(m.get("type") or "")
        try:
            ts = int(m.get("timestamp") or 0)
        except (TypeError, ValueError):
            ts = 0
        base = {"message_id": str(m["id"])[:160], "address": address, "name": names.get(sender, ""), "timestamp": ts, "raw_type": typ}
        if typ == "text":
            return Inbound(kind="text", text=str((m.get("text") or {}).get("body") or ""), **base)
        if typ == "interactive":
            inter = m.get("interactive") or {}
            reply = inter.get("button_reply") or inter.get("list_reply") or {}
            return Inbound(kind="button", button_id=str(reply.get("id") or ""), text=str(reply.get("title") or ""), **base)
        if typ == "button":  # a quick-reply button on a template message
            btn = m.get("button") or {}
            return Inbound(kind="button", button_id=str(btn.get("payload") or ""), text=str(btn.get("text") or ""), **base)
        if typ in {"image", "document", "audio", "video", "sticker"}:
            media = m.get(typ) or {}
            kind = "audio" if typ == "audio" else "image" if typ in {"image", "sticker"} else "document"
            return Inbound(
                kind=kind,
                media_id=str(media.get("id") or ""),
                mime_type=str(media.get("mime_type") or ""),
                filename=str(media.get("filename") or ""),
                text=str(media.get("caption") or ""),
                extra={"voice": bool(media.get("voice"))},
                **base,
            )
        return Inbound(kind="other", **base)

    # ---- outbound ----
    async def _post(self, body: dict) -> str:
        url = f"{self.base}/{self.s.whatsapp_phone_number_id}/messages"
        payload = {"messaging_product": "whatsapp", **body}
        try:
            if self._client is not None:
                resp = await self._client.post(url, json=payload, headers=self._headers())
            else:
                async with httpx.AsyncClient(timeout=20) as client:
                    resp = await client.post(url, json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            raise SendError(f"WhatsApp send failed: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            code = ""
            try:
                err = (resp.json() or {}).get("error") or {}
                code = f"{err.get('code', '')} {err.get('error_subcode', '')}".strip()
            except ValueError:
                pass
            raise SendError(f"WhatsApp send failed: HTTP {resp.status_code} {code}".strip())
        try:
            return str(((resp.json() or {}).get("messages") or [{}])[0].get("id") or "")
        except (ValueError, IndexError, AttributeError):
            return ""

    async def send_text(self, to: str, text: str) -> str:
        return await self._post(
            {"recipient_type": "individual", "to": to.lstrip("+"), "type": "text", "text": {"body": text[:TEXT_MAX], "preview_url": False}}
        )

    async def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]]) -> str:
        rows = [{"type": "reply", "reply": {"id": bid[:256], "title": button_title(title)}} for bid, title in buttons[:BUTTON_MAX]]
        return await self._post(
            {
                "recipient_type": "individual",
                "to": to.lstrip("+"),
                "type": "interactive",
                "interactive": {"type": "button", "body": {"text": body[:INTERACTIVE_BODY_MAX]}, "action": {"buttons": rows}},
            }
        )

    async def send_media(self, to: str, url: str, kind: str, caption: str = "", filename: str = "") -> str:
        kind = kind if kind in {"image", "document", "audio", "video"} else "document"
        media: dict = {"link": url}
        if caption and kind != "audio":
            media["caption"] = caption[:1024]
        if kind == "document" and filename:
            media["filename"] = filename[:200]
        return await self._post({"recipient_type": "individual", "to": to.lstrip("+"), "type": kind, kind: media})

    async def send_template(self, to: str, name: str, lang: str, params: list[str]) -> str:
        template: dict = {"name": name, "language": {"code": lang or "en"}}
        if params:
            template["components"] = [{"type": "body", "parameters": [{"type": "text", "text": p[:1000]} for p in params]}]
        return await self._post({"to": to.lstrip("+"), "type": "template", "template": template})

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        """Graph gives a short-lived URL for the media id; the bytes need the same bearer token."""
        limit = get_settings().attachment_max_bytes
        async with httpx.AsyncClient(timeout=30) as client:
            info = await client.get(f"{self.base}/{media_id}", headers=self._headers())
            if info.status_code >= 400:
                raise SendError(f"WhatsApp media lookup failed: HTTP {info.status_code}")
            data = info.json() or {}
            url = str(data.get("url") or "")
            if not url.startswith("https://"):
                raise SendError("WhatsApp media has no download URL")
            if int(data.get("file_size") or 0) > limit:
                raise SendError("That file is larger than the 25 MB limit")
            resp = await client.get(url, headers=self._headers())
            if resp.status_code >= 400:
                raise SendError(f"WhatsApp media download failed: HTTP {resp.status_code}")
            if len(resp.content) > limit:
                raise SendError("That file is larger than the 25 MB limit")
            return resp.content, str(data.get("mime_type") or resp.headers.get("content-type") or "application/octet-stream")


TWILIO_TEXT_MAX = 1600
TWILIO_API = "https://api.twilio.com/2010-04-01"
_MEDIA_HOSTS = ("https://api.twilio.com/", "https://media.twiliocdn.com/")


def twilio_signature(auth_token: str, url: str, params) -> str:
    """X-Twilio-Signature: base64(HMAC-SHA1(auth token, full URL + every POST key and value sorted by key))."""
    items = params.items() if isinstance(params, dict) else params
    data = url + "".join(f"{k}{v}" for k, v in sorted(((str(k), str(v)) for k, v in items), key=lambda kv: kv))
    return base64.b64encode(hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def check_twilio_signature(auth_token: str, url: str, params, header: str | None) -> bool:
    if not auth_token or not header:
        return False
    return hmac.compare_digest(twilio_signature(auth_token, url, params), header.strip())


def wa_address(raw: str) -> str:
    """'whatsapp:+14155238886' / '+1 415 523 8886' -> 'whatsapp:+14155238886' ('' when not a number)."""
    number = e164(str(raw or "").removeprefix("whatsapp:"))
    return f"whatsapp:{number}" if number else ""


def split_text(text: str, limit: int) -> list[str]:
    from app.channels.formatting import chunk

    return chunk(text, limit) or ([text] if text else [])


class TwilioWhatsApp:
    """WhatsApp through a Twilio sender. Inbound arrives form-encoded at /api/channels/whatsapp/twilio."""

    name = "whatsapp"
    kind = "twilio"
    text_max = TWILIO_TEXT_MAX
    webhook_path = "/api/channels/whatsapp/twilio"

    def __init__(self, settings=None, client: httpx.AsyncClient | None = None):
        self.s = settings or get_settings()
        self._client = client

    # ---- config ----
    def enabled(self) -> bool:
        s = self.s
        return bool(s.twilio_account_sid and s.twilio_auth_token and wa_address(s.twilio_whatsapp_number))

    def business_number(self) -> str:
        return e164(str(self.s.twilio_whatsapp_number or "").removeprefix("whatsapp:"))

    def notify_template(self) -> str:
        return str(getattr(self.s, "twilio_content_sid_notify", "") or "").strip()

    def public_base(self) -> str:
        base = str(getattr(self.s, "whatsapp_public_base_url", "") or "").strip() or str(getattr(self.s, "public_web_url", "") or "")
        return base.rstrip("/")

    def webhook_url(self, path: str = "", query: str = "") -> str:
        return self.public_base() + (path or self.webhook_path) + (f"?{query}" if query else "")

    def _auth(self) -> tuple[str, str]:
        s = self.s
        if s.twilio_api_key_sid and s.twilio_api_key_secret:
            return (s.twilio_api_key_sid, s.twilio_api_key_secret)
        return (s.twilio_account_sid, s.twilio_auth_token)

    # ---- inbound ----
    def verify_signature(self, raw: bytes, header: str | None) -> bool:  # Meta-style raw-body check does not apply
        return False

    def validate(self, url: str, params, header: str | None) -> bool:
        return check_twilio_signature(self.s.twilio_auth_token, url, params, header)

    def parse(self, form) -> list[Inbound]:
        """One Twilio webhook = one message. ``form`` is a dict (or pairs) of the POST fields."""
        f = dict(form.items() if isinstance(form, dict) else form)
        sid = str(f.get("MessageSid") or f.get("SmsMessageSid") or "")
        address = e164(str(f.get("From") or "").removeprefix("whatsapp:"))
        if not sid or not address or not str(f.get("From") or "").startswith("whatsapp:"):
            return []
        ours = self.business_number()
        to = e164(str(f.get("To") or "").removeprefix("whatsapp:"))
        if ours and to and to != ours:
            return []  # another sender on the same account
        base = {"message_id": sid[:160], "address": address, "name": str(f.get("ProfileName") or "")[:120], "raw_type": str(f.get("MessageType") or "")}
        body = str(f.get("Body") or "")
        payload = str(f.get("ButtonPayload") or "")
        if payload:
            return [Inbound(kind="button", button_id=payload, text=str(f.get("ButtonText") or body), **base)]
        try:
            num_media = int(f.get("NumMedia") or 0)
        except (TypeError, ValueError):
            num_media = 0
        if num_media > 0 and f.get("MediaUrl0"):
            mime = str(f.get("MediaContentType0") or "")
            kind = "audio" if mime.startswith("audio/") else "image" if mime.startswith("image/") else "document"
            extra = {"voice": kind == "audio"}
            if num_media > 1:
                extra["more_media"] = [str(f.get(f"MediaUrl{i}") or "") for i in range(1, min(num_media, 10))]
            return [Inbound(kind=kind, media_id=str(f["MediaUrl0"]), mime_type=mime, text=body, extra=extra, **base)]
        if body:
            return [Inbound(kind="text", text=body, **base)]
        return [Inbound(kind="other", **base)]

    # ---- outbound ----
    def message_payload(self, to: str, **fields) -> dict:
        data = {"From": wa_address(self.s.twilio_whatsapp_number), "To": wa_address(to)}
        if getattr(self.s, "twilio_status_callback", False) and self.public_base():
            data["StatusCallback"] = self.webhook_url(self.webhook_path + "/status")
        data.update({k: v for k, v in fields.items() if v not in (None, "")})
        return data

    async def _post(self, data: dict) -> str:
        url = f"{TWILIO_API}/Accounts/{self.s.twilio_account_sid}/Messages.json"
        try:
            if self._client is not None:
                resp = await self._client.post(url, data=data, auth=self._auth())
            else:
                async with httpx.AsyncClient(timeout=20) as client:
                    resp = await client.post(url, data=data, auth=self._auth())
        except httpx.HTTPError as exc:
            raise SendError(f"Twilio send failed: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            code = ""
            try:
                code = str((resp.json() or {}).get("code") or "")
            except ValueError:
                pass
            raise SendError(f"Twilio send failed: HTTP {resp.status_code} {code}".strip())
        try:
            return str((resp.json() or {}).get("sid") or "")
        except ValueError:
            return ""

    async def send_text(self, to: str, text: str) -> str:
        sid = ""
        for part in split_text(text, TWILIO_TEXT_MAX):
            sid = await self._post(self.message_payload(to, Body=part))
        return sid

    async def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]]) -> str:
        """Approvals use the quick-reply Content template when configured; otherwise (and for option lists) the
        choices go as text and the reply ("yes", "no", "2", or the option's name) is parsed by the service."""
        buttons = list(buttons)[:10]
        ids = [str(b[0]) for b in buttons]
        approval = len(ids) == 2 and ids[0].startswith("wa:ap:") and ids[1].startswith("wa:dn:")
        template = str(getattr(self.s, "twilio_content_sid_approval", "") or "").strip()
        if approval and template:
            variables = {"1": body[:INTERACTIVE_BODY_MAX], "2": ids[0], "3": ids[1]}
            return await self._post(self.message_payload(to, ContentSid=template, ContentVariables=json.dumps(variables)))
        if approval:
            text = f"{body}\n\nReply *YES* to approve or *NO* to deny."
        else:
            options = "\n".join(f"{i}. {title}" for i, (_, title) in enumerate(buttons, 1))
            text = f"{body}\n\n{options}\n\nReply with a number or the option."
        return await self.send_text(to, text)

    async def send_media(self, to: str, url: str, kind: str, caption: str = "", filename: str = "") -> str:
        body = caption if kind != "audio" else ""
        if not body and filename and kind == "document":
            body = filename
        return await self._post(self.message_payload(to, MediaUrl=url, Body=body[:TWILIO_TEXT_MAX]))

    async def send_template(self, to: str, name: str, lang: str, params: list[str]) -> str:
        """``name`` is a Content SID (HX...); params fill {{1}}, {{2}}, ..."""
        variables = {str(i): str(p)[:1000] for i, p in enumerate(params, 1)}
        return await self._post(self.message_payload(to, ContentSid=name, ContentVariables=json.dumps(variables) if variables else ""))

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        """``media_id`` is the MediaUrl Twilio sent; fetching it needs the account's basic auth."""
        url = str(media_id or "")
        if not url.startswith(_MEDIA_HOSTS) or not re.match(r"^https://[^/]+/", url):
            raise SendError("Unexpected media URL")
        limit = get_settings().attachment_max_bytes
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url, auth=self._auth())
            if resp.status_code in (401, 403) and self._auth()[0] != self.s.twilio_account_sid:
                resp = await client.get(url, auth=(self.s.twilio_account_sid, self.s.twilio_auth_token))
            if resp.status_code >= 400:
                raise SendError(f"Twilio media download failed: HTTP {resp.status_code}")
            if len(resp.content) > limit:
                raise SendError("That file is larger than the 25 MB limit")
            return resp.content, str(resp.headers.get("content-type") or "application/octet-stream").split(";")[0]


def choose(settings=None) -> str:
    s = settings or get_settings()
    want = str(getattr(s, "whatsapp_provider", "") or "").strip().lower()
    if want in {"twilio", "meta"}:
        return want
    return "twilio" if getattr(s, "twilio_account_sid", "") else "meta"


_provider: ChannelProvider | None = None


def get_provider() -> ChannelProvider:
    global _provider
    if _provider is None:
        _provider = TwilioWhatsApp() if choose() == "twilio" else MetaWhatsApp()
    return _provider


def set_provider(provider: ChannelProvider | None) -> None:
    """Tests swap in a fake provider."""
    global _provider
    _provider = provider
