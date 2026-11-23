"""Which model, key and provider a call uses (docs/LLD.md "Model gateway").

Every model call in Lumio asks ``resolve(ctx, purpose)`` for a ``ModelSpec`` and builds its model or client from it:
the agent's chat model (``chat_model``), the small background calls in ``app.llm.chat_complete``, and the OpenAI
clients for images, transcription and computer use (``openai_client``). Today every spec is Lumio's own OpenAI key and
the models configured in settings, unless the user picked their own key and model on the Models page
(app/model_keys), or one agent's own choice: a run loads those into ``CHOICES`` (``enter``) and ``resolve`` uses them
for the agent and background purposes. Later phases add team choices and other providers; ``resolve`` stays
the one place the choice is made, in this order: the agent's setting, the user's default, the team's, then Lumio's.
"""

from __future__ import annotations

import re
from contextvars import ContextVar
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


# The choices for the running agent ({purpose: ModelSpec}), set by ``enter`` when a run starts: the agent's own
# (its About panel), else the user's default (Models page). Work the run starts sees them too; work outside a run
# uses Lumio's models.
CHOICES: ContextVar[dict | None] = ContextVar("model_choices", default=None)
# Every choice the user made ({scope: {purpose: ModelSpec}}, scope "user" or an agent id), loaded once by the outermost
# run, so a teammate's nested run picks its own agent's model without asking the database again.
ALL_CHOICES: ContextVar[dict | None] = ContextVar("all_model_choices", default=None)


def _chosen(ctx: Any, purpose: str) -> ModelSpec | None:
    """The model picked for ``purpose``. Background work uses the background model, else the agent model, so a user
    who brought a key isn't billed on Lumio's for half the turn. Team choices come in a later phase."""
    choices = CHOICES.get() or {}
    if purpose == "agent":
        return choices.get("agent")
    if purpose == "small":
        spec = choices.get("small") or choices.get("agent")
        return replace(spec, purpose="small") if spec is not None else None
    return None


def for_agent(all_choices: dict | None, agent_id: Any = None) -> dict:
    """The choices one agent runs with: its own for each purpose, else the user's."""
    every = all_choices or {}
    own = every.get(str(agent_id), {}) if agent_id is not None else {}
    mine = every.get("user", {})
    return {p: own.get(p) or mine.get(p) for p in ("agent", "small") if own.get(p) or mine.get(p)}


async def enter(db: Any, user: Any, agent: Any = None) -> list:
    """Set the model choices for one run of ``agent``; give the result to ``leave`` when the run ends. Never raises:
    a run that can't load them runs on Lumio's models."""
    import logging

    tokens: list = []
    every = ALL_CHOICES.get()
    if every is None:
        every = {}
        if db is not None and getattr(user, "id", None) is not None:
            try:
                from app.model_keys.service import load_all_choices

                every = await load_all_choices(db, user.id)
            except Exception as exc:  # noqa: BLE001
                logging.getLogger(__name__).warning("model choices not loaded: %s", type(exc).__name__)
        tokens.append((ALL_CHOICES, ALL_CHOICES.set(every)))
    tokens.append((CHOICES, CHOICES.set(for_agent(every, getattr(agent, "id", None)))))
    return tokens


def leave(tokens: list) -> None:
    for var, token in reversed(tokens or []):
        var.reset(token)


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


# Reasoning effort a model accepts, learned from its own error when it refused Lumio's setting (REASONING_EFFORT, often
# "none"): {model: effort}, or OMIT_EFFORT for a model that takes no reasoning setting at all. Nothing is listed by
# hand: a new model teaches Lumio on its first refused call, and that call is retried.
_EFFORT: dict[str, str] = {}
OMIT_EFFORT = "-"
_EFFORT_REFUSED = re.compile(r"reasoning|effort|'none' is not supported", re.I)
_SUPPORTED = re.compile(r"supported values are:?\s*(.+?)(?:\.|$)", re.I | re.S)
_NO_REASONING = re.compile(r"unsupported parameter:?\s*'?reasoning", re.I)


def effort_for(model: str | None = None) -> str:
    """The reasoning effort to send ``model``: what it accepted before, else REASONING_EFFORT."""
    learned = _EFFORT.get((model or "").strip())
    if learned:
        return learned
    return (get_settings().openai_reasoning_effort or "").strip() or "none"


def reasoning_for(model: str | None = None) -> dict[str, str] | None:
    """The Responses API ``reasoning`` field for ``model``, or None to leave it out."""
    effort = effort_for(model)
    if effort == OMIT_EFFORT:
        return None
    if effort == "none":
        return {"effort": "none"}
    return {"effort": effort, "mode": "standard", "summary": "auto"}


def learn_effort(model: str | None, exc: BaseException) -> bool:
    """When ``exc`` is a model refusing the reasoning effort Lumio sent, remember what it takes and say True (retry)."""
    name = (model or "").strip()
    text = str(exc or "")
    lowered = text.lower()
    if not name or not ("400" in text or "invalid_request" in lowered or "unsupported" in lowered):
        return False
    if not _EFFORT_REFUSED.search(text):
        return False
    if _NO_REASONING.search(text):
        _EFFORT[name] = OMIT_EFFORT
        return True
    found = _SUPPORTED.search(text)
    allowed = re.findall(r"'([a-z]+)'", found.group(1)) if found else []
    current = effort_for(name)
    if not allowed or current in allowed:
        return False
    # The cheapest it allows when Lumio asked for none or a lower one; otherwise the closest it allows.
    _EFFORT[name] = allowed[0]
    return True


def chat_model(spec: ModelSpec, *, cache_key: str = "", streaming: bool = False, timeout: int = 120) -> Any:
    """The LangChain chat model the agent runs on."""
    if not spec.is_openai:
        raise UnsupportedProvider(f"agents can't run on {spec.provider} yet")
    from langchain_openai import ChatOpenAI

    effort = effort_for(spec.model)
    reasoning = reasoning_for(spec.model)
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
    if reasoning is None:  # the model takes no reasoning setting
        attempts = [{k: v for k, v in a.items() if k not in ("reasoning", "reasoning_effort")} for a in attempts]
    for extra in attempts:
        try:
            return ChatOpenAI(**base, **extra)
        except TypeError:
            continue
    return ChatOpenAI(**base)


def small_chat_model(ctx: Any = None, timeout: int = 60) -> Any:
    """A LangChain model for background work (memory extraction): the small model, no streaming, no cache key."""
    return chat_model(resolve(ctx, "small"), timeout=timeout)


OWN_KEY = " (your key)"


def usage_name(model: str) -> str:
    """The name usage is recorded under: a model the user runs on their own key gets OWN_KEY, so Usage shows it apart
    and MODEL_PRICES (Lumio's cost and the daily cap) doesn't price it."""
    name = str(model or "")
    if not name or name.endswith(OWN_KEY):
        return name
    own = {spec.model for spec in (CHOICES.get() or {}).values() if not spec.is_platform}
    if not own:
        return name
    return name + OWN_KEY if name in own or any(name.startswith(m + "-20") for m in own) else name


def record_usage(spec: ModelSpec, usage_obj: Any) -> None:
    """Count the tokens one call used, under the model that served it."""
    from app.agent import usage

    if spec.is_openai or spec.provider in ("openrouter", "openai_compatible", "groq", "xai"):
        usage.from_openai(spec.model, usage_obj)
