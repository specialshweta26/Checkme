"""Model providers a user can bring a key for, and how a key is checked (docs/LLD.md "Model gateway").

A key is checked when it is saved by asking the provider for its model list: a cheap call that costs nothing and
fails for a wrong or revoked key. The list it returns is what the Models page offers to pick from.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

MAX_MODELS = 500
TIMEOUT = 12.0


class KeyCheckFailed(ValueError):
    """The provider refused the key, or couldn't be reached. The message is safe to show."""


@dataclass(frozen=True)
class Provider:
    key: str
    name: str
    base_url: str  # OpenAI-compatible address, "" when the user supplies it
    runs: bool  # Lumio can run agents on it
    hint: str  # where to get a key
    custom_url: bool = False


PROVIDERS: dict[str, Provider] = {
    p.key: p
    for p in (
        Provider("openai", "OpenAI", "https://api.openai.com/v1", True, "platform.openai.com → API keys"),
        Provider("anthropic", "Anthropic", "https://api.anthropic.com/v1", True, "console.anthropic.com → API keys"),
        Provider("google", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", True,
                 "aistudio.google.com → Get API key"),
        Provider("xai", "xAI (Grok)", "https://api.x.ai/v1", True, "console.x.ai → API keys"),
        Provider("groq", "Groq", "https://api.groq.com/openai/v1", True, "console.groq.com → API keys"),
        Provider("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", True, "openrouter.ai → Keys"),
        Provider("openai_compatible", "OpenAI-compatible", "", True,
                 "Any service with an OpenAI-style /models and chat API (Mistral, Together, DeepSeek, …)", custom_url=True),
    )
}  # fmt: skip

# Providers list every model they have; these can't hold a conversation, so they aren't offered.
_NOT_CHAT = ("embedding", "embed-", "whisper", "tts", "dall-e", "moderation", "transcribe", "audio", "realtime", "image",
             "davinci", "babbage", "search", "similarity", "sora", "guard", "imagen", "veo")  # fmt: skip


def public() -> list[dict]:
    return [
        {"key": p.key, "name": p.name, "runs": p.runs, "hint": p.hint, "custom_url": p.custom_url}
        for p in PROVIDERS.values()
    ]


def get(key: str) -> Provider:
    p = PROVIDERS.get((key or "").strip().lower())
    if p is None:
        raise KeyCheckFailed(f"Unknown provider: {key}")
    return p


def _ids(rows: Any, field: str = "id") -> list[str]:
    out = []
    for row in rows or []:
        value = row.get(field) if isinstance(row, dict) else None
        if isinstance(value, str) and value:
            out.append(value)
    return out


def chat_models(provider: str, ids: list[str]) -> list[str]:
    keep = [m for m in ids if not any(word in m.lower() for word in _NOT_CHAT)]
    return sorted(set(keep))[:MAX_MODELS]


def _refused(resp: httpx.Response, name: str) -> KeyCheckFailed:
    if resp.status_code in (401, 403):
        return KeyCheckFailed(f"{name} didn't accept this key.")
    if resp.status_code == 429:
        return KeyCheckFailed(f"{name} says this key is out of quota or rate limited.")
    return KeyCheckFailed(f"{name} answered {resp.status_code} when Lumio checked the key.")


async def check(provider: str, api_key: str, base_url: str = "", client: httpx.AsyncClient | None = None) -> list[str]:
    """The chat models ``api_key`` can use. Raises KeyCheckFailed with a message safe to show."""
    p = get(provider)
    key = (api_key or "").strip()
    if not key:
        raise KeyCheckFailed("Paste the API key.")
    base = (base_url or p.base_url).rstrip("/")
    if not base:
        raise KeyCheckFailed("This provider needs its API address (for example https://api.mistral.ai/v1).")
    own = client is None
    http = client or httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False)
    try:
        if p.key == "anthropic":
            resp = await http.get(f"{base}/models", params={"limit": 1000},
                                  headers={"x-api-key": key, "anthropic-version": "2023-06-01"})  # fmt: skip
            if resp.status_code != 200:
                raise _refused(resp, p.name)
            return chat_models(p.key, _ids(resp.json().get("data")))
        if p.key == "google":
            resp = await http.get("https://generativelanguage.googleapis.com/v1beta/models", params={"pageSize": 1000},
                                  headers={"x-goog-api-key": key})  # fmt: skip
            if resp.status_code != 200:
                raise _refused(resp, p.name)
            rows = [m for m in resp.json().get("models") or [] if "generateContent" in (m.get("supportedGenerationMethods") or [])]
            return chat_models(p.key, [name.removeprefix("models/") for name in _ids(rows, "name")])
        headers = {"Authorization": f"Bearer {key}"}
        if p.key == "openrouter":
            # Its model list is public, so it says nothing about the key: ask about the key itself first.
            who = await http.get(f"{base}/key", headers=headers)
            if who.status_code != 200:
                raise _refused(who, p.name)
        resp = await http.get(f"{base}/models", headers=headers)
        if resp.status_code != 200:
            raise _refused(resp, p.name)
        body = resp.json()
        return chat_models(p.key, _ids(body.get("data") if isinstance(body, dict) else body))
    except httpx.HTTPError as exc:
        raise KeyCheckFailed(f"Lumio couldn't reach {p.name}: {type(exc).__name__}.") from exc
    except ValueError as exc:
        if isinstance(exc, KeyCheckFailed):
            raise
        raise KeyCheckFailed(f"{p.name} sent an answer Lumio couldn't read.") from exc
    finally:
        if own:
            await http.aclose()
