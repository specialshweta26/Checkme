"""Which model, key and provider a call uses (docs/LLD.md "Model gateway").

Every model call in Lumio asks ``resolve(ctx, purpose)`` for a ``ModelSpec`` and builds its model or client from it:
the agent's chat model (``chat_model``), the small background calls in ``app.llm.chat_complete``, and the OpenAI
clients for images, transcription and computer use (``openai_client``). Today every spec is Lumio's own OpenAI key and
the models configured in settings, so nothing behaves differently. Later phases let a user save their own keys
(OpenAI, Anthropic, Google, xAI, Groq, OpenRouter or any OpenAI-compatible address) and pick a model for their
profile or for one agent; ``resolve`` is the one place that choice is made, in this order: the agent's setting, the
user's default, the team's default, then Lumio's.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from app.config import get_settings

# What a call needs the model for. Each purpose can later point at a different model or key.
PURPOSES = ("agent", "small", "vision", "computer_use", "image", "transcribe", "embed")
PROVIDERS = ("openai", "anthropic", "google", "xai", "groq", "openrouter", "openai_compatible")
PLATFORM = "platform"


class UnsupportedProvider(RuntimeError):
    """A spec names a provider Lumio can't call yet."""


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str
    api_key: str = field(default="", repr=False)  # never printed or logged
    base_url: str = ""
    purpose: str = "agent"
    source: str = PLATFORM  # platform | user | team | agent: whose setting chose this

    @property
    def is_openai(self) -> bool:
        return self.provider == "openai"

    @property
    def is_platform(self) -> bool:
        return self.source == PLATFORM

    def describe(self) -> dict[str, str]:
        """Safe to show or log: no key."""
        return {"provider": self.provider, "model": self.model, "purpose": self.purpose, "source": self.source}


def _platform_model(purpose: str) -> str:
    s = get_settings()
    main = (s.openai_model or "").strip()
    if purpose == "small":
        return (s.openai_small_model or "").strip() or main
    if purpose == "computer_use":
        return str(getattr(s, "computer_use_model", "") or "").strip() or main
    if purpose == "embed":
        return (s.openai_embedding_model or "").strip()
    if purpose == "transcribe":
        return (getattr(s, "transcribe_model", "") or "").strip() or "whisper-1"
    return main


def _chosen(ctx: Any, purpose: str) -> ModelSpec | None:
    """A model the agent, user or team picked for ``purpose``. None until saved keys exist (later phase)."""
    return None


def resolve(ctx: Any = None, purpose: str = "agent", model: str | None = None) -> ModelSpec:
    """The spec for one call. ``model`` names a specific model (a caller's own setting such as BROWSER_FAST_MODEL); it
    keeps the provider and key that would have been used."""
    if purpose not in PURPOSES:
        raise ValueError(f"unknown model purpose: {purpose}")
    spec = _chosen(ctx, purpose) or ModelSpec(
        provider="openai",
        model=_platform_model(purpose),
        api_key=get_settings().openai_api_key or "",
        purpose=purpose,
    )
    wanted = (model or "").strip()
    return replace(spec, model=wanted) if wanted else spec


_CLIENTS: dict[tuple[str, str], Any] = {}


def openai_client(spec: ModelSpec | None = None) -> Any:
    """An OpenAI SDK client for ``spec``: Lumio's shared one for its own key, otherwise one per key and address."""
    from app import llm

    if spec is None or spec.is_platform:
        return llm.openai_client()
    if spec.provider not in ("openai", "openrouter", "openai_compatible", "groq", "xai"):
        raise UnsupportedProvider(f"{spec.provider} has no OpenAI-compatible client")
    from openai import AsyncOpenAI

    key = (spec.api_key, spec.base_url)
    client = _CLIENTS.get(key)
    if client is None:
        client = AsyncOpenAI(api_key=spec.api_key, base_url=spec.base_url or None, timeout=60, max_retries=1)
        _CLIENTS[key] = client
    return client


def chat_model(spec: ModelSpec, *, cache_key: str = "", streaming: bool = False, timeout: int = 120) -> Any:
    """The LangChain chat model the agent runs on."""
    if not spec.is_openai:
        raise UnsupportedProvider(f"agents can't run on {spec.provider} yet")
    from langchain_openai import ChatOpenAI

    from app.llm import reasoning_payload

    s = get_settings()
    effort = (s.openai_reasoning_effort or "").strip() or "none"
    reasoning = reasoning_payload()
    base: dict[str, Any] = {"model": spec.model, "api_key": spec.api_key, "timeout": timeout, "max_retries": 1}
    if spec.base_url:
        base["base_url"] = spec.base_url
    if cache_key:
        base["model_kwargs"] = {"prompt_cache_key": cache_key}
    if streaming:
        # Tokens reach the callbacks as they are generated (the chat shows the reply as it streams).
        base["streaming"] = True
    # gpt-6-luna function tools + reasoning only work on /v1/responses; older langchain-openai versions reject some of
    # these arguments, so each is dropped in turn.
    attempts = [
        {"use_responses_api": True, "reasoning": reasoning, "verbosity": "medium", "service_tier": "default"},
        {"use_responses_api": True, "reasoning": reasoning, "verbosity": "medium"},
        {"use_responses_api": True, "reasoning": reasoning},
        {"use_responses_api": True, "reasoning_effort": effort},
        {"use_responses_api": True},
    ]
    for extra in attempts:
        try:
            return ChatOpenAI(**base, **extra)
        except TypeError:
            continue
    return ChatOpenAI(**base)


def small_chat_model(ctx: Any = None, timeout: int = 60) -> Any:
    """A LangChain model for background work (memory extraction): the small model, no streaming, no cache key."""
    return chat_model(resolve(ctx, "small"), timeout=timeout)


def record_usage(spec: ModelSpec, usage_obj: Any) -> None:
    """Count the tokens one call used, under the model that served it."""
    from app.agent import usage

    if spec.is_openai or spec.provider in ("openrouter", "openai_compatible", "groq", "xai"):
        usage.from_openai(spec.model, usage_obj)
