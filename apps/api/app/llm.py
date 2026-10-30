from __future__ import annotations

import hashlib
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
        _client = AsyncOpenAI(api_key=s.openai_api_key)
    return _client


def reasoning_payload() -> dict[str, str]:
    effort = (get_settings().openai_reasoning_effort or "").strip() or "none"
    if effort == "none":
        return {"effort": "none"}
    return {"effort": effort, "mode": "standard", "summary": "auto"}


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
) -> Any:
    """Call /v1/responses. gpt-6-luna cannot use function tools on chat completions with reasoning."""
    instructions, items = _split_messages(messages)
    kwargs: dict[str, Any] = {
        "model": get_settings().openai_model,
        "service_tier": "default",
        "input": items,
        "text": _text_format(response_format),
        "reasoning": reasoning_payload(),
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
    resp = await openai_client().responses.create(**kwargs)
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
        resp = await openai_client().embeddings.create(
            model=get_settings().openai_embedding_model,
            input=[t for _, t in missing],
        )
        for (i, _text), item in zip(missing, resp.data, strict=True):
            vec = list(item.embedding)
            cached[i] = vec
            _emb_put(_emb_key(_text), vec)
    return [v or [] for v in cached]


async def embed_one(text: str) -> list[float]:
    return (await embed_texts([text]))[0]
