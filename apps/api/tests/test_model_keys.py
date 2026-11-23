"""Phase 2 of the model gateway: a user's own model keys and the model their agents use (docs/LLD.md "Model gateway").

The provider checks run against a fake HTTP transport. The DB part needs CHECKME_TEST_DATABASE_URL."""

from __future__ import annotations

import os
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app import llm, llm_gateway
from app.config import get_settings
from app.model_keys import providers

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")


def _transport(routes: dict):
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        for (host, path), (status, body) in routes.items():
            if request.url.host == host and request.url.path == path:
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={})

    return httpx.MockTransport(handle), seen


async def _check(provider, routes, key="sk-test-1234", base_url=""):
    transport, seen = _transport(routes)
    async with httpx.AsyncClient(transport=transport) as client:
        return await providers.check(provider, key, base_url, client=client), seen


# --- checking a key with its provider ----------------------------------------------------------------------------


async def test_openai_key_lists_chat_models_only():
    models, seen = await _check("openai", {("api.openai.com", "/v1/models"): (200, {"data": [
        {"id": "gpt-6-luna"}, {"id": "text-embedding-3-small"}, {"id": "whisper-1"}, {"id": "gpt-5-mini"}]})})  # fmt: skip
    assert models == ["gpt-5-mini", "gpt-6-luna"]
    assert seen[0].headers["authorization"] == "Bearer sk-test-1234"


async def test_anthropic_and_google_use_their_own_headers():
    models, seen = await _check("anthropic", {("api.anthropic.com", "/v1/models"): (200, {"data": [{"id": "claude-x"}]})})
    assert models == ["claude-x"] and seen[0].headers["x-api-key"] == "sk-test-1234" and "authorization" not in seen[0].headers
    models, seen = await _check("google", {("generativelanguage.googleapis.com", "/v1beta/models"): (200, {"models": [
        {"name": "models/gemini-pro", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/embedding-001", "supportedGenerationMethods": ["embedContent"]}]})})  # fmt: skip
    assert models == ["gemini-pro"] and seen[0].headers["x-goog-api-key"] == "sk-test-1234"


async def test_openrouter_checks_the_key_not_just_the_public_model_list():
    routes = {("openrouter.ai", "/api/v1/key"): (401, {}), ("openrouter.ai", "/api/v1/models"): (200, {"data": [{"id": "m"}]})}
    with pytest.raises(providers.KeyCheckFailed, match="didn't accept"):
        await _check("openrouter", routes)


@pytest.mark.parametrize(("status", "words"), [(401, "didn't accept"), (429, "quota"), (500, "answered 500")])
async def test_refusals_say_why_in_plain_words(status, words):
    with pytest.raises(providers.KeyCheckFailed, match=words):
        await _check("groq", {("api.groq.com", "/openai/v1/models"): (status, {})})


async def test_unknown_provider_and_empty_key_are_refused():
    with pytest.raises(providers.KeyCheckFailed):
        await providers.check("nope", "k")
    with pytest.raises(providers.KeyCheckFailed, match="Paste"):
        await providers.check("openai", " ")


def test_only_openai_runs_agents_today():
    assert [p["key"] for p in providers.public() if p["runs"]] == ["openai"]


# --- the gateway uses the user's choice for the turn -------------------------------------------------------------


@pytest.fixture
def platform(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "openai_api_key", "sk-platform")
    monkeypatch.setattr(s, "openai_model", "main-model")
    monkeypatch.setattr(s, "openai_small_model", "small-model")
    return s


def _mine(model="my-model", purpose="agent"):
    return llm_gateway.ModelSpec(provider="openai", model=model, api_key="sk-mine", purpose=purpose, source="user")


def test_a_turn_with_a_choice_runs_on_the_users_key(platform):
    token = llm_gateway.CHOICES.set({"agent": _mine()})
    try:
        agent = llm_gateway.resolve(None, "agent")
        assert (agent.model, agent.api_key, agent.source) == ("my-model", "sk-mine", "user")
        small = llm_gateway.resolve(None, "small")  # no background choice: the agent model, on the user's key
        assert (small.model, small.api_key, small.purpose) == ("my-model", "sk-mine", "small")
        assert llm_gateway.resolve(None, "embed").api_key == "sk-platform"  # embeddings stay on Lumio's
        assert llm_gateway.resolve(None, "computer_use").api_key == "sk-platform"
        model = llm_gateway.chat_model(agent)
        assert model.model_name == "my-model" and model.openai_api_key.get_secret_value() == "sk-mine"
    finally:
        llm_gateway.CHOICES.reset(token)
    assert llm_gateway.resolve(None, "agent").api_key == "sk-platform"


def test_a_background_choice_wins_for_background_work(platform):
    token = llm_gateway.CHOICES.set({"agent": _mine(), "small": _mine("my-small", "small")})
    try:
        assert llm_gateway.resolve(None, "small").model == "my-small"
    finally:
        llm_gateway.CHOICES.reset(token)


def test_usage_on_the_users_key_is_labelled(platform):
    assert llm_gateway.usage_name("my-model") == "my-model"
    token = llm_gateway.CHOICES.set({"agent": _mine()})
    try:
        assert llm_gateway.usage_name("my-model") == "my-model (your key)"
        assert llm_gateway.usage_name("my-model-2026-01-01") == "my-model-2026-01-01 (your key)"
        assert llm_gateway.usage_name("my-model (your key)") == "my-model (your key)"
        assert llm_gateway.usage_name("text-embedding-3-small") == "text-embedding-3-small"
    finally:
        llm_gateway.CHOICES.reset(token)


async def test_chat_complete_uses_a_client_for_the_users_key(platform, monkeypatch):
    seen = {}

    class Responses:
        async def create(self, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(output_text="ok", usage=None)

    monkeypatch.setattr(llm, "openai_client", lambda: pytest.fail("Lumio's client used for the user's key"))
    monkeypatch.setattr(llm_gateway, "openai_client", lambda spec=None: seen.setdefault("key", spec.api_key) and SimpleNamespace(responses=Responses()))
    token = llm_gateway.CHOICES.set({"agent": _mine()})
    try:
        await llm.chat_complete([{"role": "user", "content": "x"}], model=llm.small_model())
    finally:
        llm_gateway.CHOICES.reset(token)
    assert seen["key"] == "sk-mine" and seen["model"] == "my-model"


async def test_use_choices_never_breaks_a_turn(platform, monkeypatch):
    from app.model_keys import service

    async def boom(db, user_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(service, "load_choices", boom)
    token = await llm_gateway.use_choices(object(), SimpleNamespace(id="u"))
    try:
        assert llm_gateway.resolve(None, "agent").api_key == "sk-platform"
    finally:
        llm_gateway.CHOICES.reset(token)


# --- saving keys and choices (Postgres) --------------------------------------------------------------------------

if URL:
    from tests.test_user_memory_db import _user_with_agents, factory  # noqa: F401 - the fixture is used by name


def _client(factory, user):
    from app.db import get_db
    from app.deps import current_user
    from app.model_keys.routes import router

    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def db_dep():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db] = db_dep
    app.dependency_overrides[current_user] = lambda: user
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app.test")


@pytest.fixture
def fake_check(monkeypatch):
    async def check(provider, api_key, base_url="", client=None):
        if api_key.endswith("bad"):
            raise providers.KeyCheckFailed("OpenAI didn't accept this key.")
        return ["gpt-mine", "gpt-other"]

    monkeypatch.setattr(providers, "check", check)


@needs_db
async def test_keys_are_checked_encrypted_and_never_sent_back(factory, fake_check, platform):  # noqa: F811
    from sqlalchemy import select

    from app.model_keys.service import load_choices
    from app.models import ModelKey

    user, _ = await _user_with_agents(factory)
    other, _ = await _user_with_agents(factory)
    async with _client(factory, user) as c:
        bad = await c.post("/api/models/keys", json={"provider": "openai", "api_key": "sk-is-bad"})
        assert bad.status_code == 400 and "didn't accept" in bad.json()["detail"]
        saved = await c.post("/api/models/keys", json={"provider": "openai", "api_key": "sk-secret-9876", "label": "Work"})
        assert saved.status_code == 200, saved.text
        row = saved.json()
        assert row["hint"] == "9876" and row["status"] == "ok" and row["models"] == ["gpt-mine", "gpt-other"]
        page = await c.get("/api/models")
        assert "sk-secret-9876" not in page.text and page.json()["keys"][0]["label"] == "Work"

        refused = await c.put("/api/models/choices/agent", json={"key_id": row["id"], "model": "not-on-this-key"})
        assert refused.status_code == 400
        chose = await c.put("/api/models/choices/agent", json={"key_id": row["id"], "model": "gpt-mine"})
        assert chose.json()["choices"]["agent"] == {"key_id": row["id"], "model": "gpt-mine"}

    async with factory() as db:
        stored = (await db.execute(select(ModelKey).where(ModelKey.user_id == user.id))).scalar_one()
        assert "sk-secret" not in stored.key_enc
        specs = await load_choices(db, user.id)
        assert specs["agent"].api_key == "sk-secret-9876" and specs["agent"].model == "gpt-mine"
        assert await load_choices(db, other.id) == {}

    async with _client(factory, other) as c:  # another user can't see or use it
        assert (await c.get("/api/models")).json()["keys"] == []
        assert (await c.put("/api/models/choices/agent", json={"key_id": row["id"], "model": "gpt-mine"})).status_code == 400
        assert (await c.delete(f"/api/models/keys/{row['id']}")).status_code == 400

    async with _client(factory, user) as c:
        gone = await c.delete(f"/api/models/keys/{row['id']}")
        assert gone.json()["keys"] == [] and gone.json()["choices"] == {}
    async with factory() as db:
        assert await load_choices(db, user.id) == {}


@needs_db
async def test_a_provider_agents_cant_run_on_yet_is_saved_but_not_chosen(factory, fake_check, platform):  # noqa: F811
    user, _ = await _user_with_agents(factory)
    async with _client(factory, user) as c:
        row = (await c.post("/api/models/keys", json={"provider": "anthropic", "api_key": "sk-ant-1111"})).json()
        assert row["runs"] is False and row["status"] == "ok"
        refused = await c.put("/api/models/choices/agent", json={"key_id": row["id"], "model": "gpt-mine"})
        assert refused.status_code == 400 and "can't run agents on Anthropic yet" in refused.json()["detail"]
        back = await c.put("/api/models/choices/agent", json={"key_id": None})
        assert back.status_code == 200 and back.json()["choices"] == {}


@needs_db
async def test_custom_addresses_must_be_public(factory, fake_check, platform, monkeypatch):  # noqa: F811
    monkeypatch.setattr(get_settings(), "allow_private_mcp_urls", False)
    user, _ = await _user_with_agents(factory)
    async with _client(factory, user) as c:
        res = await c.post("/api/models/keys", json={"provider": "openai_compatible", "api_key": "k-1", "base_url": "https://127.0.0.1/v1"})
        assert res.status_code == 400 and "can't be used" in res.json()["detail"]
