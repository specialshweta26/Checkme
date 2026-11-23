"""What a model can do: call tools, read images, PDFs and audio, how much it can read at once (docs/LLD.md "Model
gateway", phase 5).

Nothing is listed by hand. Two public catalogs are read once a day: LiteLLM's model list (OpenAI, Anthropic, Gemini,
xAI, Groq and more) and OpenRouter's (its own models). A model neither lists is "unknown" (None) for each ability, and
Lumio learns from the model's own errors: a model that refuses tools or images is remembered as unable to.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

REFRESH_SECONDS = 24 * 3600
RETRY_SECONDS = 15 * 60
FETCH_TIMEOUT = 15.0
# LiteLLM's prefix for each provider's models, in its catalog keys ("gemini/gemini-2.5-pro").
_PREFIX = {"google": ("gemini/", "vertex_ai/"), "xai": ("xai/",), "groq": ("groq/",), "openrouter": ("openrouter/",),
           "anthropic": ("anthropic/",), "openai": ("openai/",)}  # fmt: skip


@dataclass(frozen=True)
class Caps:
    tools: bool | None = None
    images: bool | None = None
    pdf: bool | None = None
    audio: bool | None = None
    context: int | None = None  # input tokens
    max_output: int | None = None

    def public(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


_CATALOG: dict[str, Caps] = {}
_STATE = {"loaded_at": 0.0, "tried_at": 0.0}
_LOCK = asyncio.Lock()
_LEARNED: dict[str, dict[str, bool]] = {}  # model -> {ability: False}, from the model's own errors


def _int(value: Any) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _from_litellm(data: dict) -> dict[str, Caps]:
    out: dict[str, Caps] = {}
    for name, row in (data or {}).items():
        if not isinstance(row, dict) or row.get("mode") not in (None, "chat", "responses"):
            continue
        out[name.lower()] = Caps(
            tools=row.get("supports_function_calling"),
            images=row.get("supports_vision"),
            pdf=row.get("supports_pdf_input"),
            audio=row.get("supports_audio_input"),
            context=_int(row.get("max_input_tokens") or row.get("max_tokens")),
            max_output=_int(row.get("max_output_tokens")),
        )
    return out


def _from_openrouter(data: dict) -> dict[str, Caps]:
    out: dict[str, Caps] = {}
    for row in (data or {}).get("data") or []:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        params = row.get("supported_parameters")
        inputs = ((row.get("architecture") or {}).get("input_modalities")) or []
        top = row.get("top_provider") or {}
        out["openrouter/" + str(row["id"]).lower()] = Caps(
            tools=("tools" in params) if isinstance(params, list) else None,
            images=("image" in inputs) if inputs else None,
            pdf=("file" in inputs) if inputs else None,
            audio=("audio" in inputs) if inputs else None,
            context=_int(row.get("context_length") or top.get("context_length")),
            max_output=_int(top.get("max_completion_tokens")),
        )
    return out


async def _fetch(client: httpx.AsyncClient, url: str) -> Any:
    if not url:
        return None
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001 - a missing catalog only means "unknown"
        log.warning("model catalog not loaded from %s: %s", url.split("/")[2] if "//" in url else url, type(exc).__name__)
        return None


async def refresh(force: bool = False, client: httpx.AsyncClient | None = None) -> None:
    """Load the catalogs when they are older than a day (or failed more than 15 minutes ago)."""
    now = time.time()
    if not force and (now - _STATE["loaded_at"] < REFRESH_SECONDS or now - _STATE["tried_at"] < RETRY_SECONDS):
        return
    async with _LOCK:
        if not force and now - _STATE["tried_at"] < RETRY_SECONDS:
            return
        _STATE["tried_at"] = now
        s = get_settings()
        own = client is None
        http = client or httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True)
        try:
            litellm, router = await asyncio.gather(
                _fetch(http, getattr(s, "model_catalog_url", "")), _fetch(http, getattr(s, "openrouter_catalog_url", ""))
            )
        finally:
            if own:
                await http.aclose()
        found = {**_from_litellm(litellm if isinstance(litellm, dict) else {}), **_from_openrouter(router if isinstance(router, dict) else {})}
        if found:
            _CATALOG.clear()
            _CATALOG.update(found)
            _STATE["loaded_at"] = now


def ensure_loading() -> None:
    """Start a refresh in the background if one is due; never waits (a turn doesn't pay for it)."""
    now = time.time()
    if now - _STATE["loaded_at"] < REFRESH_SECONDS or now - _STATE["tried_at"] < RETRY_SECONDS:
        return
    try:
        asyncio.get_running_loop().create_task(refresh())
    except RuntimeError:
        pass


def _strip_date(model: str) -> str:
    return re.sub(r"-(\d{8}|\d{4}-\d{2}-\d{2}|latest)$", "", model)


def lookup(provider: str, model: str) -> Caps:
    """What ``model`` on ``provider`` can do, as far as the catalogs and its own errors say."""
    name = (model or "").strip().lower()
    found = None
    if name:
        names = [name, _strip_date(name)]
        candidates = [p + n for p in _PREFIX.get(provider, ()) for n in names] + names
        if provider == "openrouter":
            candidates = ["openrouter/" + n for n in names] + candidates
            # An OpenRouter id is "vendor/model": the vendor's own entry describes the same model.
            candidates += [n.split("/", 1)[1] for n in names if "/" in n]
        found = next((_CATALOG[c] for c in candidates if c in _CATALOG), None)
    caps = found or Caps()
    learned = _LEARNED.get(name)
    if learned:
        caps = Caps(**{**asdict(caps), **learned})
    return caps


_NO_TOOLS = re.compile(r"(does not|doesn't|do not) support (tools|tool use|tool calling|function calling)|"
                       r"(tools?|tool use|tool calling|function calling|tool_choice) (is|are) not supported|"
                       r"tool use is not enabled|unsupported parameter:?\s*'?tools", re.I)  # fmt: skip
_NO_IMAGES = re.compile(r"(does not|doesn't|do not) support (image|images|vision|image input)|"
                        r"(image|images|image input|vision|image_url)( content)? (is|are) not supported|"
                        r"model is not multimodal|cannot (read|process|accept) images", re.I)  # fmt: skip


def learn(model: str | None, exc: BaseException) -> str | None:
    """When ``exc`` says ``model`` can't take tools or images, remember it and say which ("tools" or "images")."""
    name = (model or "").strip().lower()
    text = str(exc or "")
    if not name:
        return None
    for ability, pattern in (("tools", _NO_TOOLS), ("images", _NO_IMAGES)):
        if pattern.search(text):
            _LEARNED.setdefault(name, {})[ability] = False
            return ability
    return None


def summary(provider: str, models: list[str]) -> dict[str, dict]:
    """{model: abilities} for the models a key lists, only those something is known about."""
    out = {}
    for model in models or []:
        caps = lookup(provider, model).public()
        if caps:
            out[model] = caps
    return out
