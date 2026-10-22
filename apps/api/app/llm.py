from __future__ import annotations

import hashlib
import json
from typing import Any

from openai import AsyncOpenAI

from app.cache import get_redis
from app.config import get_settings

_client: AsyncOpenAI | None = None


def openai_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        s = get_settings()
        _client = AsyncOpenAI(api_key=s.openai_api_key)
    return _client


def _chat_kwargs() -> dict[str, Any]:
    effort = (get_settings().openai_reasoning_effort or "").strip()
    if not effort:
        return {}
    return {"reasoning_effort": effort}


async def chat_complete(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict] | None = None,
    tool_choice: str | dict | None = None,
    temperature: float | None = None,
    response_format: dict | None = None,
    max_tokens: int = 1200,
) -> Any:
    kwargs: dict[str, Any] = {
        "model": get_settings().openai_model,
        "messages": messages,
        "max_completion_tokens": max_tokens,
        **_chat_kwargs(),
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice or "auto"
    if temperature is not None:
        kwargs["temperature"] = temperature
    if response_format:
        kwargs["response_format"] = response_format
    return await openai_client().chat.completions.create(**kwargs)


async def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    r = get_redis()
    cached: list[list[float] | None] = []
    missing: list[tuple[int, str]] = []
    for i, text in enumerate(texts):
        key = "emb:" + hashlib.sha256(text.encode()).hexdigest()
        hit = await r.get(key)
        if hit:
            cached.append(json.loads(hit))
        else:
            cached.append(None)
            missing.append((i, text))
    if missing:
        resp = await openai_client().embeddings.create(
            model=get_settings().openai_embedding_model,
            input=[t for _, t in missing],
        )
        for (i, text), item in zip(missing, resp.data, strict=True):
            vec = list(item.embedding)
            cached[i] = vec
            key = "emb:" + hashlib.sha256(text.encode()).hexdigest()
            await r.set(key, json.dumps(vec), ex=7 * 86400)
    return [v or [] for v in cached]


async def embed_one(text: str) -> list[float]:
    return (await embed_texts([text]))[0]
