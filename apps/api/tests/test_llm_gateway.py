"""The model gateway picks the model and key for every call (docs/LLD.md "Model gateway"). Phase 1: Lumio's own key."""

import uuid
from types import SimpleNamespace

import pytest

from app import llm, llm_gateway
from app.agent import deep
from app.config import get_settings


@pytest.fixture
def settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "openai_api_key", "sk-platform")
    monkeypatch.setattr(s, "openai_model", "main-model")
    monkeypatch.setattr(s, "openai_small_model", "small-model")
    monkeypatch.setattr(s, "computer_use_model", "")
    return s


def test_every_purpose_uses_lumios_key_and_configured_models(settings):
    for purpose in llm_gateway.PURPOSES:
        spec = llm_gateway.resolve(None, purpose)
        assert spec.provider == "openai" and spec.api_key == "sk-platform" and spec.is_platform
    assert llm_gateway.resolve(None, "agent").model == "main-model"
    assert llm_gateway.resolve(None, "small").model == "small-model"
    assert llm_gateway.resolve(None, "computer_use").model == "main-model"
    assert llm_gateway.resolve(None, "embed").model == settings.openai_embedding_model


def test_small_falls_back_to_main_model(settings, monkeypatch):
    monkeypatch.setattr(settings, "openai_small_model", "")
    assert llm_gateway.resolve(None, "small").model == "main-model"


def test_a_named_model_keeps_provider_and_key(settings):
    spec = llm_gateway.resolve(None, "agent", model=" fast-model ")
    assert (spec.model, spec.api_key, spec.provider) == ("fast-model", "sk-platform", "openai")


def test_unknown_purpose_is_refused(settings):
    with pytest.raises(ValueError):
        llm_gateway.resolve(None, "dreams")


def test_the_key_is_never_printed(settings):
    spec = llm_gateway.resolve(None, "agent")
    assert "sk-platform" not in repr(spec) and "sk-platform" not in str(spec.describe())


def test_agent_model_is_unchanged(settings):
    ctx = SimpleNamespace(agent=SimpleNamespace(id=uuid.uuid4()), user=None)
    model = deep._chat_model(ctx)
    assert model.model_name == "main-model" and model.use_responses_api
    assert model.openai_api_key.get_secret_value() == "sk-platform"
    assert model.model_kwargs.get("prompt_cache_key") == deep.cache_key(ctx)


def test_other_providers_say_they_are_not_supported_yet(settings):
    spec = llm_gateway.ModelSpec(provider="anthropic", model="claude", api_key="k", source="user")
    with pytest.raises(llm_gateway.UnsupportedProvider):
        llm_gateway.chat_model(spec)
    with pytest.raises(llm_gateway.UnsupportedProvider):
        llm_gateway.openai_client(spec)


def test_platform_specs_share_lumios_client_and_user_keys_get_their_own(settings, monkeypatch):
    monkeypatch.setattr(llm, "_client", None)
    platform = llm_gateway.openai_client(llm_gateway.resolve(None, "agent"))
    assert platform is llm.openai_client()
    mine = llm_gateway.ModelSpec(provider="openrouter", model="m", api_key="sk-user", base_url="https://openrouter.ai/api/v1",
                                 source="user")
    client = llm_gateway.openai_client(mine)
    assert client is not platform and client is llm_gateway.openai_client(mine)
    assert str(client.base_url).startswith("https://openrouter.ai/api/v1") and client.max_retries == 1
    llm_gateway._CLIENTS.clear()


async def test_chat_complete_asks_the_gateway(settings, monkeypatch):
    seen = {}

    class Responses:
        async def create(self, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(output_text="hi", usage=None)

    monkeypatch.setattr(llm, "openai_client", lambda: SimpleNamespace(responses=Responses()))
    asked = []
    real = llm_gateway.resolve
    monkeypatch.setattr(llm_gateway, "resolve", lambda ctx=None, purpose="agent", model=None: asked.append(purpose) or real(ctx, purpose, model))
    out = await llm.chat_complete([{"role": "user", "content": "x"}], model=llm.small_model())
    assert out.choices[0].message.content == "hi" and seen["model"] == "small-model" and asked == ["small"]
    await llm.chat_complete([{"role": "user", "content": "x"}])
    assert seen["model"] == "main-model" and asked[-1] == "agent"
    await llm.chat_complete([{"role": "user", "content": "x"}], model="fast-model")
    assert seen["model"] == "fast-model" and asked[-1] == "agent"
