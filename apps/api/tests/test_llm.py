from types import SimpleNamespace

from app.agent import fast_browser
from app.config import get_settings
from app.llm import (
    small_model,
    _EMB_LOCAL,
    _as_chat_completion,
    _responses_tools,
    _split_messages,
    _text_format,
    embed_texts,
    reasoning_payload,
)


def test_reasoning_payload_shape():
    payload = reasoning_payload()
    assert payload["effort"]
    if payload["effort"] == "none":
        assert "summary" not in payload
    else:
        assert payload["mode"] == "standard"
        assert payload["summary"] == "auto"


def test_split_messages_moves_system_to_instructions():
    instructions, items = _split_messages(
        [
            {"role": "system", "content": "You are staff."},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"},
        ]
    )
    assert instructions == "You are staff."
    assert items == [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello"},
    ]


def test_responses_tools_unwrap_chat_completions_function():
    tools = _responses_tools(
        [{"type": "function", "function": {"name": "search_tools", "description": "Find tools", "parameters": {"type": "object"}}}]
    )
    assert tools == [
        {
            "type": "function",
            "name": "search_tools",
            "description": "Find tools",
            "parameters": {"type": "object"},
        }
    ]


def test_text_format_json_and_plain():
    assert _text_format({"type": "json_object"})["format"]["type"] == "json_object"
    assert _text_format(None)["format"]["type"] == "text"
    assert _text_format(None)["verbosity"] == "medium"


def test_as_chat_completion_keeps_caller_shape():
    resp = _as_chat_completion("ok")
    assert resp.choices[0].message.content == "ok"


async def test_embed_texts_uses_process_cache_not_redis(monkeypatch):
    _EMB_LOCAL.clear()
    calls = []

    class _Emb:
        def __init__(self, vec):
            self.embedding = vec

    class _Embeddings:
        async def create(self, **kwargs):
            calls.append(kwargs["input"])
            return SimpleNamespace(data=[_Emb([0.1, 0.2]) for _ in kwargs["input"]])

    monkeypatch.setattr("app.llm.openai_client", lambda: SimpleNamespace(embeddings=_Embeddings()))
    first = await embed_texts(["hello"])
    second = await embed_texts(["hello", "hello"])
    assert first == [[0.1, 0.2]]
    assert second == [[0.1, 0.2], [0.1, 0.2]]
    assert calls == [["hello"]]


def test_small_model_falls_back_to_the_main_model(monkeypatch):
    monkeypatch.setattr(get_settings(), "openai_model", "main-model")
    monkeypatch.setattr(get_settings(), "openai_small_model", "")
    assert small_model() == "main-model"
    monkeypatch.setattr(get_settings(), "openai_small_model", "tiny-model")
    assert small_model() == "tiny-model"


def test_browser_fast_model_prefers_its_own_setting_then_small_model(monkeypatch):
    monkeypatch.setattr(get_settings(), "openai_small_model", "tiny-model")
    monkeypatch.setattr(get_settings(), "browser_fast_model", "")
    assert fast_browser.fast_model() == "tiny-model"
    monkeypatch.setattr(get_settings(), "browser_fast_model", "browser-model")
    assert fast_browser.fast_model() == "browser-model"
