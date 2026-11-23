"""Phase 5 of the model gateway: what each model can do, and OpenAI's processing tiers (docs/LLD.md "Model gateway")."""

from __future__ import annotations

import os
from types import SimpleNamespace

import httpx
import pytest

from app import llm, llm_gateway
from app.config import get_settings
from app.model_keys import capabilities as caps

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")

LITELLM = {
    "sample_spec": {"mode": "chat"},
    "gpt-6.1-sol": {"mode": "responses", "supports_function_calling": True, "supports_vision": True, "max_input_tokens": 400000},
    "claude-x-4": {"mode": "chat", "supports_function_calling": True, "supports_vision": True, "supports_pdf_input": True, "max_input_tokens": 200000},
    "groq/tiny-text": {"mode": "chat", "supports_function_calling": False, "supports_vision": False, "max_input_tokens": 8192},
    "gemini/gemini-x-pro": {"mode": "chat", "supports_function_calling": True, "supports_vision": True, "supports_audio_input": True},
    "text-embedding-x": {"mode": "embedding"},
}
ROUTER = {"data": [{"id": "vendor/router-model", "context_length": 128000, "supported_parameters": ["tools", "temperature"],
                    "architecture": {"input_modalities": ["text", "image"]}},
                   {"id": "vendor/plain", "supported_parameters": ["temperature"], "architecture": {"input_modalities": ["text"]}}]}  # fmt: skip


@pytest.fixture
def catalog(monkeypatch):
    monkeypatch.setattr(caps, "_CATALOG", {})
    monkeypatch.setattr(caps, "_LEARNED", {})
    monkeypatch.setattr(caps, "_STATE", {"loaded_at": 0.0, "tried_at": 0.0})
    monkeypatch.setattr(get_settings(), "model_catalog_url", "https://catalog.test/litellm.json")
    monkeypatch.setattr(get_settings(), "openrouter_catalog_url", "https://catalog.test/router.json")

    def handle(request):
        return httpx.Response(200, json=LITELLM if request.url.path.endswith("litellm.json") else ROUTER)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


async def test_both_catalogs_are_read_and_matched_by_provider(catalog):
    await caps.refresh(force=True, client=catalog)
    sol = caps.lookup("openai", "gpt-6.1-sol")
    assert sol.tools and sol.images and sol.context == 400000
    dated = caps.lookup("anthropic", "claude-x-4-20260101")  # a dated id matches its undated entry
    assert dated.pdf and dated.context == 200000
    assert caps.lookup("groq", "tiny-text").tools is False
    assert caps.lookup("google", "gemini-x-pro").audio is True
    assert caps.lookup("openrouter", "vendor/router-model").tools and caps.lookup("openrouter", "vendor/router-model").images
    assert caps.lookup("openrouter", "vendor/plain").tools is False
    assert caps.lookup("openai", "text-embedding-x").tools is None  # not a chat model: not listed
    assert caps.lookup("xai", "never-heard-of").public() == {}


async def test_a_catalog_that_fails_leaves_abilities_unknown(monkeypatch, catalog):
    broken = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    await caps.refresh(force=True, client=broken)
    assert caps.lookup("openai", "gpt-6.1-sol").public() == {}


def test_a_model_teaches_lumio_what_it_cant_do(catalog):
    assert caps.learn("tiny", RuntimeError("Error code: 400 - This model does not support tools")) == "tools"
    assert caps.lookup("groq", "tiny").tools is False
    assert caps.learn("tiny", RuntimeError("Error code: 400 - image input is not supported for this model")) == "images"
    assert caps.lookup("groq", "tiny").images is False
    assert caps.learn("tiny", RuntimeError("Error code: 429 - rate limited")) is None


async def test_the_key_lists_what_each_of_its_models_can_do(catalog):
    await caps.refresh(force=True, client=catalog)
    out = caps.summary("openai", ["gpt-6.1-sol", "unknown-model"])
    assert out == {"gpt-6.1-sol": {"tools": True, "images": True, "context": 400000}}


# --- processing tiers --------------------------------------------------------------------------------------------


def _own(tier="", provider="openai", model="gpt-6.1-sol"):
    return llm_gateway.ModelSpec(provider=provider, model=model, api_key="sk-mine", source="user", tier=tier)


@pytest.fixture
def tiers(monkeypatch):
    monkeypatch.setattr(llm_gateway, "_TIER_OFF", {})
    monkeypatch.setattr(llm_gateway, "_EFFORT", {})
    monkeypatch.setattr(get_settings(), "openai_api_key", "sk-platform")


def test_tiers_only_on_the_users_own_openai_key(tiers):
    assert llm_gateway.tier_for(_own("flex")) == "flex"
    assert llm_gateway.tier_for(_own("priority")) == "priority"
    assert llm_gateway.tier_for(_own("")) == "default"
    assert llm_gateway.tier_for(_own("flex", provider="groq")) == "default"
    assert llm_gateway.tier_for(llm_gateway.ModelSpec(provider="openai", model="m", tier="flex")) == "default"  # Lumio's key
    assert llm_gateway.tier_for(_own("batch")) == "default"


def test_a_refused_or_busy_tier_falls_back_to_standard_for_a_while(tiers):
    flex = _own("flex")
    assert llm_gateway.learn_tier(flex, RuntimeError("Error code: 429 - Resource unavailable for flex processing"))
    assert llm_gateway.tier_for(flex) == "default"
    assert llm_gateway.tier_for(_own("priority")) == "priority"  # only that tier on that model
    fast = _own("ultrafast")
    assert llm_gateway.learn_tier(fast, RuntimeError("Error code: 400 - Invalid value for service_tier: 'ultrafast'"))
    assert not llm_gateway.learn_tier(_own(""), RuntimeError("Error code: 400 - Invalid value for service_tier"))
    assert not llm_gateway.learn_tier(_own("priority"), RuntimeError("Error code: 401 - bad key"))


def test_the_agent_model_carries_the_tier(tiers):
    assert llm_gateway.chat_model(_own("priority")).service_tier == "priority"
    assert llm_gateway.chat_model(llm_gateway.ModelSpec(provider="openai", model="m", api_key="k")).service_tier == "default"


async def test_chat_complete_retries_a_refused_tier_on_standard(tiers, monkeypatch):
    sent = []

    class Responses:
        async def create(self, **kwargs):
            sent.append(kwargs["service_tier"])
            if kwargs["service_tier"] == "flex":
                raise RuntimeError("Error code: 429 - Resource unavailable for flex processing")
            return SimpleNamespace(output_text="ok", usage=None)

    monkeypatch.setattr(llm_gateway, "openai_client", lambda spec=None: SimpleNamespace(responses=Responses()))
    token = llm_gateway.CHOICES.set({"agent": _own("flex")})
    try:
        out = await llm.chat_complete([{"role": "user", "content": "x"}])
    finally:
        llm_gateway.CHOICES.reset(token)
    assert out.choices[0].message.content == "ok" and sent == ["flex", "default"]


# --- images for a model that can't read them ----------------------------------------------------------------------


def test_the_run_knows_whether_its_model_reads_images(catalog, tiers):
    from app.agent import deep

    assert deep._reads_images()  # Lumio's own model
    caps._LEARNED["tiny"] = {"images": False}
    token = llm_gateway.CHOICES.set({"agent": _own(model="tiny", provider="groq")})
    try:
        assert not deep._reads_images()
    finally:
        llm_gateway.CHOICES.reset(token)
    token = llm_gateway.CHOICES.set({"agent": _own(model="unknown", provider="groq")})
    try:
        assert deep._reads_images()  # unknown: try, and learn if it refuses
    finally:
        llm_gateway.CHOICES.reset(token)


async def test_images_are_described_with_lumios_vision_model(tiers, monkeypatch):
    from app.agent import attachments

    asked = {}

    class Responses:
        async def create(self, **kwargs):
            asked.update(kwargs)
            return SimpleNamespace(output_text="1. A sales table: Lotto 1,200 units.", usage=None)

    monkeypatch.setattr(attachments, "get_bytes", lambda key: (b"img", "image/png"))
    monkeypatch.setattr(attachments, "_as_data_url", lambda data: "data:image/jpeg;base64,AAAA")
    monkeypatch.setattr(llm, "openai_client", lambda: SimpleNamespace(responses=Responses()))
    msg = await attachments.described_image_message(["u/1.png"])
    assert "Lumio described them" in msg.content and "Lotto 1,200" in msg.content
    assert asked["input"][0]["content"][1]["type"] == "input_image"


# --- choosing (Postgres) ------------------------------------------------------------------------------------------

if URL:
    from tests.test_model_keys import _client, fake_check  # noqa: F401
    from tests.test_user_memory_db import _user_with_agents, factory  # noqa: F401


@needs_db
async def test_choosing_checks_tools_and_tiers(factory, fake_check, tiers, catalog):  # noqa: F811
    from app.model_keys.service import load_choices

    caps._LEARNED["gpt-other"] = {"tools": False}
    user, _ = await _user_with_agents(factory)
    async with _client(factory, user) as c:
        key = (await c.post("/api/models/keys", json={"provider": "openai", "api_key": "sk-tier-7777"})).json()
        no_tools = await c.put("/api/models/choices/agent", json={"key_id": key["id"], "model": "gpt-other"})
        assert no_tools.status_code == 400 and "can't call tools" in no_tools.json()["detail"]
        ok_small = await c.put("/api/models/choices/small", json={"key_id": key["id"], "model": "gpt-other"})
        assert ok_small.status_code == 200  # background work doesn't need tools
        bad_tier = await c.put("/api/models/choices/agent", json={"key_id": key["id"], "model": "gpt-mine", "tier": "batch"})
        assert bad_tier.status_code == 400
        fast = await c.put("/api/models/choices/agent", json={"key_id": key["id"], "model": "gpt-mine", "tier": "priority"})
        assert fast.json()["choices"]["agent"]["tier"] == "priority" and {"key": "flex", "name": "Flex"} in fast.json()["tiers"]
        other = (await c.post("/api/models/keys", json={"provider": "anthropic", "api_key": "sk-ant-8888"})).json()
        refused = await c.put("/api/models/choices/agent", json={"key_id": other["id"], "model": "gpt-mine", "tier": "flex"})
        assert refused.status_code == 400 and "OpenAI keys" in refused.json()["detail"]
    async with factory() as db:
        assert (await load_choices(db, user.id))["agent"].tier == "priority"
