"""Channel providers: a small interface so another provider (e.g. Twilio) can be added next to Meta.

Only MetaWhatsApp exists today. It talks to the WhatsApp Business Cloud API (graph.facebook.com) directly.
Nothing here logs a token, the app secret or a message body."""

from __future__ import annotations

import hashlib
import hmac
import logging
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

    def __init__(self, settings=None, client: httpx.AsyncClient | None = None):
        self.s = settings or get_settings()
        self._client = client

    # ---- config ----
    def enabled(self) -> bool:
        s = self.s
        return bool(s.whatsapp_phone_number_id and s.whatsapp_access_token and s.whatsapp_app_secret and s.whatsapp_verify_token)

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


_provider: ChannelProvider | None = None


def get_provider() -> ChannelProvider:
    global _provider
    if _provider is None:
        _provider = MetaWhatsApp()
    return _provider


def set_provider(provider: ChannelProvider | None) -> None:
    """Tests swap in a fake provider."""
    global _provider
    _provider = provider
