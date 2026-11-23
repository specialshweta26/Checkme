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


def test_every_provider_runs_agents():
    assert all(p["runs"] for p in providers.public())


def _spec(provider, model="m", base_url=""):
    return llm_gateway.ModelSpec(provider=provider, model=model, api_key="sk-user", base_url=base_url, source="user")


def test_each_provider_gets_its_own_chat_model():
    from langchain_anthropic import ChatAnthropic
    from langchain_openai import ChatOpenAI

    claude = llm_gateway.chat_model(_spec("anthropic", "claude-x"), streaming=True)
    assert isinstance(claude, ChatAnthropic) and claude.model == "claude-x" and claude.streaming
    assert claude.max_tokens == llm_gateway.AGENT_MAX_TOKENS
    for provider in llm_gateway.COMPATIBLE:
        base = providers.PROVIDERS[provider].base_url or "https://api.example.com/v1"
        model = llm_gateway.chat_model(_spec(provider, "grok-x", base), cache_key="lumio-k")
        assert isinstance(model, ChatOpenAI) and model.openai_api_base == base and not model.use_responses_api
        assert not (model.model_kwargs or {}).get("prompt_cache_key") and not getattr(model, "reasoning", None)
    with pytest.raises(llm_gateway.UnsupportedProvider):
        llm_gateway.chat_model(_spec("groq"))  # no address


def test_lumios_model_names_dont_reach_another_provider(platform):
    token = llm_gateway.CHOICES.set({"agent": _spec("anthropic", "claude-x")})
    try:
        assert llm_gateway.resolve(None, "agent", model="gpt-5.4-nano").model == "claude-x"
    finally:
        llm_gateway.CHOICES.reset(token)


async def test_background_calls_run_on_another_providers_model(platform, monkeypatch):
    from langchain_core.messages import AIMessage

    asked = {}

    class Fake:
        def bind(self, **kw):
            asked["bound"] = kw
            return self

        async def ainvoke(self, convo):
            asked["convo"] = convo
            return AIMessage(content=[{"type": "text", "text": 'Sure: ```json\n{"ok": true}\n```'}],
                             usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})

    monkeypatch.setattr(llm_gateway, "chat_model", lambda spec, **kw: asked.setdefault("kw", kw) and Fake())
    counted = []
    monkeypatch.setattr("app.agent.usage.add", lambda *a: counted.append(a))
    token = llm_gateway.CHOICES.set({"agent": _spec("anthropic", "claude-x", "https://api.anthropic.com/v1")})
    try:
        out = await llm.chat_complete([{"role": "system", "content": "Rules"}, {"role": "user", "content": "Go"}],
                                      model=llm.small_model(), response_format={"type": "json_object"}, max_tokens=300)
    finally:
        llm_gateway.CHOICES.reset(token)
    assert out.choices[0].message.content == '{"ok": true}'
    assert "JSON object only" in asked["convo"][0].content and asked["convo"][1].content == "Go"
    assert asked["kw"]["max_tokens"] == 300 and counted == [("claude-x", 7, 3, 0)]


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


async def test_loading_choices_never_breaks_a_run(platform, monkeypatch):
    from app.model_keys import service

    async def boom(db, user_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(service, "load_all_choices", boom)
    tokens = await llm_gateway.enter(object(), SimpleNamespace(id="u"))
    try:
        assert llm_gateway.resolve(None, "agent").api_key == "sk-platform"
    finally:
        llm_gateway.leave(tokens)


def test_an_agents_own_choice_wins_over_the_users_default():
    every = {"user": {"agent": _mine("default-model"), "small": _mine("default-small", "small")},
             "a1": {"agent": _mine("coder-model")}}  # fmt: skip
    a1 = llm_gateway.for_agent(every, "a1")
    assert a1["agent"].model == "coder-model" and a1["small"].model == "default-small"
    assert llm_gateway.for_agent(every, "a2")["agent"].model == "default-model"
    assert llm_gateway.for_agent(every, None)["agent"].model == "default-model"
    assert llm_gateway.for_agent({"a1": {"agent": _mine("coder-model")}}, "a2") == {}


async def test_a_teammate_runs_on_its_own_model_without_loading_again(platform, monkeypatch):
    from app.model_keys import service

    loads = []

    async def load(db, user_id):
        loads.append(user_id)
        return {"user": {"agent": _mine("default-model")}, "coder": {"agent": _mine("coder-model")}}

    monkeypatch.setattr(service, "load_all_choices", load)
    user = SimpleNamespace(id="u")
    outer = await llm_gateway.enter(object(), user, SimpleNamespace(id="chief"))
    try:
        assert llm_gateway.resolve(None, "agent").model == "default-model"
        inner = await llm_gateway.enter(object(), user, SimpleNamespace(id="coder"))
        try:
            assert llm_gateway.resolve(None, "agent").model == "coder-model"
        finally:
            llm_gateway.leave(inner)
        assert llm_gateway.resolve(None, "agent").model == "default-model"
    finally:
        llm_gateway.leave(outer)
    assert loads == ["u"] and llm_gateway.resolve(None, "agent").model == "main-model"
    assert llm_gateway.ALL_CHOICES.get() is None and llm_gateway.CHOICES.get() is None


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
        assert chose.json()["choices"]["agent"] == {"key_id": row["id"], "model": "gpt-mine", "tier": ""}

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
async def test_an_anthropic_key_runs_agents_at_its_address(factory, fake_check, platform):  # noqa: F811
    from app.model_keys.service import load_choices

    user, _ = await _user_with_agents(factory)
    async with _client(factory, user) as c:
        row = (await c.post("/api/models/keys", json={"provider": "anthropic", "api_key": "sk-ant-1111"})).json()
        assert row["runs"] is True and row["status"] == "ok"
        chose = await c.put("/api/models/choices/agent", json={"key_id": row["id"], "model": "gpt-mine"})
        assert chose.status_code == 200, chose.text
    async with factory() as db:
        spec = (await load_choices(db, user.id))["agent"]
        assert spec.provider == "anthropic" and spec.base_url == "https://api.anthropic.com/v1"
    async with _client(factory, user) as c:
        back = await c.put("/api/models/choices/agent", json={"key_id": None})
        assert back.status_code == 200 and back.json()["choices"] == {}


@needs_db
async def test_one_agent_gets_its_own_model(factory, fake_check, platform):  # noqa: F811
    from app.agents.service import delete_agent
    from app.model_keys.service import load_choices

    user, (chief, coder) = await _user_with_agents(factory, "Coder")
    other, (other_chief,) = await _user_with_agents(factory)
    async with _client(factory, user) as c:
        key = (await c.post("/api/models/keys", json={"provider": "openai", "api_key": "sk-agent-5555"})).json()
        await c.put("/api/models/choices/agent", json={"key_id": key["id"], "model": "gpt-other"})
        res = await c.put("/api/models/choices/agent", json={"key_id": key["id"], "model": "gpt-mine", "agent_id": str(coder.id)})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["choices"]["agent"]["model"] == "gpt-other"
        assert body["agents"] == {str(coder.id): {"agent": {"key_id": key["id"], "model": "gpt-mine", "tier": ""}}}
        refused = await c.put("/api/models/choices/agent", json={"key_id": key["id"], "model": "gpt-mine", "agent_id": str(other_chief.id)})
        assert refused.status_code == 400 and "isn't yours" in refused.json()["detail"]

    async with factory() as db:
        assert (await load_choices(db, user.id, coder.id))["agent"].model == "gpt-mine"
        assert (await load_choices(db, user.id, chief.id))["agent"].model == "gpt-other"
        assert (await load_choices(db, user.id, coder.id))["agent"].source == "agent"

    async with _client(factory, user) as c:  # back to the user's default
        back = await c.put("/api/models/choices/agent", json={"key_id": None, "agent_id": str(coder.id)})
        assert back.json()["agents"] == {} and back.json()["choices"]["agent"]["model"] == "gpt-other"
        await c.put("/api/models/choices/agent", json={"key_id": key["id"], "model": "gpt-mine", "agent_id": str(coder.id)})

    async with factory() as db:  # deleting the agent removes its choice
        from app.models import User

        await delete_agent(db, await db.get(User, user.id), coder.id)
    async with _client(factory, user) as c:
        assert (await c.get("/api/models")).json()["agents"] == {}


@needs_db
async def test_custom_addresses_must_be_public(factory, fake_check, platform, monkeypatch):  # noqa: F811
    monkeypatch.setattr(get_settings(), "allow_private_mcp_urls", False)
    user, _ = await _user_with_agents(factory)
    async with _client(factory, user) as c:
        res = await c.post("/api/models/keys", json={"provider": "openai_compatible", "api_key": "k-1", "base_url": "https://127.0.0.1/v1"})
        assert res.status_code == 400 and "can't be used" in res.json()["detail"]


# --- a model that refuses Lumio's reasoning effort teaches Lumio what it takes ----------------------------------

REFUSED = ("Error code: 400 - {'error': {'message': \"Unsupported value: 'none' is not supported with the 'gpt-6.1-sol' "
           "model. Supported values are: 'low', 'medium', 'high', 'xhigh', and 'max'.\", 'type': 'invalid_request_error', "
           "'param': 'reasoning.effort', 'code': 'unsupported_value'}}")  # fmt: skip


@pytest.fixture
def fresh_efforts(monkeypatch):
    monkeypatch.setattr(llm_gateway, "_EFFORT", {})
    monkeypatch.setattr(get_settings(), "openai_reasoning_effort", "none")


def test_a_refused_effort_is_learned_from_the_error(fresh_efforts):
    assert llm_gateway.reasoning_for("gpt-6.1-sol") == {"effort": "none"}
    assert llm_gateway.learn_effort("gpt-6.1-sol", RuntimeError(REFUSED))
    assert llm_gateway.effort_for("gpt-6.1-sol") == "low"
    assert llm_gateway.reasoning_for("gpt-6.1-sol")["effort"] == "low"
    assert llm_gateway.effort_for("gpt-6-luna") == "none"  # other models keep the setting
    assert not llm_gateway.learn_effort("gpt-6.1-sol", RuntimeError(REFUSED))  # already takes "low": nothing to learn


def test_other_errors_are_not_mistaken_for_an_effort_refusal(fresh_efforts):
    assert not llm_gateway.learn_effort("m", RuntimeError("Error code: 401 - invalid api key"))
    assert not llm_gateway.learn_effort("m", RuntimeError("Error code: 400 - context_length_exceeded"))
    assert not llm_gateway.learn_effort("", RuntimeError(REFUSED))


def test_a_model_without_reasoning_gets_none_sent(fresh_efforts, platform):
    assert llm_gateway.learn_effort("plain-model", RuntimeError("Error code: 400 - Unsupported parameter: 'reasoning.effort' is not supported with this model."))
    assert llm_gateway.reasoning_for("plain-model") is None
    model = llm_gateway.chat_model(llm_gateway.ModelSpec(provider="openai", model="plain-model", api_key="k"))
    assert not getattr(model, "reasoning", None) and not getattr(model, "reasoning_effort", None)


def test_the_agent_model_is_built_with_the_learned_effort(fresh_efforts, platform):
    llm_gateway.learn_effort("gpt-6.1-sol", RuntimeError(REFUSED))
    model = llm_gateway.chat_model(_mine("gpt-6.1-sol"))
    assert model.reasoning["effort"] == "low"


async def test_chat_complete_retries_once_with_the_effort_the_model_takes(fresh_efforts, platform, monkeypatch):
    sent = []

    class Responses:
        async def create(self, **kwargs):
            sent.append(kwargs.get("reasoning"))
            if kwargs.get("reasoning", {}).get("effort") == "none":
                raise RuntimeError(REFUSED)
            return SimpleNamespace(output_text="ok", usage=None)

    monkeypatch.setattr(llm, "openai_client", lambda: SimpleNamespace(responses=Responses()))
    out = await llm.chat_complete([{"role": "user", "content": "x"}], model="gpt-6.1-sol")
    assert out.choices[0].message.content == "ok"
    assert [r["effort"] for r in sent] == ["none", "low"]
    await llm.chat_complete([{"role": "user", "content": "x"}], model="gpt-6.1-sol")
    assert sent[-1]["effort"] == "low" and len(sent) == 3  # learned: no second refusal
