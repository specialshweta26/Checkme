from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from types import SimpleNamespace
from typing import Any

from openai import AsyncOpenAI

from app.config import get_settings

# Process-local only. Storing embedding JSON in Redis filled the 256MB instance and crashed it.
_EMB_LOCAL: OrderedDict[str, list[float]] = OrderedDict()
_EMB_LOCAL_MAX = 256

_client: AsyncOpenAI | None = None


def openai_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        s = get_settings()
        # The default waits 10 minutes per try and retries twice, so one stuck call could hang a turn for half an hour.
        _client = AsyncOpenAI(api_key=s.openai_api_key, timeout=60, max_retries=1)
    return _client


def small_model() -> str:
    """Model for background and routing calls: OPENAI_SMALL_MODEL, else OPENAI_MODEL."""
    s = get_settings()
    return (s.openai_small_model or "").strip() or s.openai_model


def reasoning_payload(model: str | None = None) -> dict[str, str]:
    """REASONING_EFFORT as the Responses API's ``reasoning`` field, or what ``model`` accepted instead
    (app/llm_gateway.py ``learn_effort``)."""
    from app.llm_gateway import reasoning_for

    return reasoning_for(model) or {}


def _as_chat_completion(text: str) -> Any:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text or ""))])


def _output_text(resp: Any) -> str:
    text = getattr(resp, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text
    chunks: list[str] = []
    for item in getattr(resp, "output", None) or []:
        for part in getattr(item, "content", None) or []:
            value = getattr(part, "text", None)
            if isinstance(value, str) and value:
                chunks.append(value)
    return "".join(chunks)


def _split_messages(messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, str]]]:
    instructions: list[str] = []
    items: list[dict[str, str]] = []
    for raw in messages:
        role = str(raw.get("role") or "user")
        content = raw.get("content")
        if not isinstance(content, str):
            content = json.dumps(content, default=str) if content is not None else ""
        if role == "system":
            if content.strip():
                instructions.append(content)
            continue
        items.append({"role": "assistant" if role == "assistant" else "user", "content": content})
    return ("\n\n".join(instructions) if instructions else None), items


def _responses_tools(tools: list[dict] | None) -> list[dict] | None:
    if not tools:
        return None
    out: list[dict] = []
    for tool in tools:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if tool.get("type") == "function" and isinstance(fn, dict):
            out.append(
                {
                    "type": "function",
                    "name": fn.get("name"),
                    "description": fn.get("description") or "",
                    "parameters": fn.get("parameters") or {},
                }
            )
        else:
            out.append(tool)
    return out


def _text_format(response_format: dict | None) -> dict[str, Any]:
    kind = (response_format or {}).get("type") if isinstance(response_format, dict) else None
    fmt: dict[str, Any] = {"type": "json_object"} if kind == "json_object" else {"type": "text"}
    return {"format": fmt, "verbosity": "medium"}


async def chat_complete(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict] | None = None,
    tool_choice: str | dict | None = None,
    temperature: float | None = None,
    response_format: dict | None = None,
    max_tokens: int = 1200,
    model: str | None = None,
    ctx: Any = None,
    purpose: str = "agent",
) -> Any:
    """Call /v1/responses. gpt-6-luna cannot use function tools on chat completions with reasoning.
    The model gateway picks the model and key (app/llm_gateway.py); ``model`` names one for this call (the fast
    browser loop's BROWSER_FAST_MODEL, or small_model() for background work)."""
    from app import llm_gateway

    if purpose == "agent" and model and model.strip() == small_model():
        # Background calls pass model=small_model(); they are the small purpose, so a user's small-model choice applies.
        purpose, model = "small", None
    spec = llm_gateway.resolve(ctx, purpose, model=model)
    if not spec.is_openai:
        raise llm_gateway.UnsupportedProvider(f"background calls can't run on {spec.provider} yet")
    instructions, items = _split_messages(messages)
    kwargs: dict[str, Any] = {
        "model": spec.model,
        "service_tier": "default",
        "input": items,
        "text": _text_format(response_format),
        "max_output_tokens": max_tokens,
    }
    if instructions:
        kwargs["instructions"] = instructions
    converted = _responses_tools(tools)
    if converted:
        kwargs["tools"] = converted
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
    if temperature is not None:
        kwargs["temperature"] = temperature
    client = openai_client() if spec.is_platform else llm_gateway.openai_client(spec)
    for attempt in range(2):
        reasoning = llm_gateway.reasoning_for(spec.model)
        if reasoning is None:
            kwargs.pop("reasoning", None)
        else:
            kwargs["reasoning"] = reasoning
        try:
            resp = await client.responses.create(**kwargs)
            break
        except Exception as exc:
            # A model that refuses the reasoning effort says what it takes: remember it and ask once more.
            if attempt == 0 and llm_gateway.learn_effort(spec.model, exc):
                continue
            raise
    llm_gateway.record_usage(spec, getattr(resp, "usage", None))
    return _as_chat_completion(_output_text(resp))


def _emb_key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _emb_get(digest: str) -> list[float] | None:
    hit = _EMB_LOCAL.get(digest)
    if hit is not None:
        _EMB_LOCAL.move_to_end(digest)
    return hit


def _emb_put(digest: str, vec: list[float]) -> None:
    _EMB_LOCAL[digest] = vec
    _EMB_LOCAL.move_to_end(digest)
    while len(_EMB_LOCAL) > _EMB_LOCAL_MAX:
        _EMB_LOCAL.popitem(last=False)


async def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    cached: list[list[float] | None] = []
    missing: list[tuple[int, str]] = []
    for i, text in enumerate(texts):
        hit = _emb_get(_emb_key(text))
        if hit is not None:
            cached.append(hit)
        else:
            cached.append(None)
            missing.append((i, text))
    if missing:
        # Embeddings always use Lumio's model: stored vectors only match vectors from the same model.
        resp = await openai_client().embeddings.create(
            model=get_settings().openai_embedding_model,
            input=[t for _, t in missing],
        )
        from app.agent import usage

        usage.from_openai(get_settings().openai_embedding_model, getattr(resp, "usage", None))
        for (i, _text), item in zip(missing, resp.data, strict=True):
            vec = list(item.embedding)
            cached[i] = vec
            _emb_put(_emb_key(_text), vec)
    return [v or [] for v in cached]


async def embed_one(text: str) -> list[float]:
    return (await embed_texts([text]))[0]
