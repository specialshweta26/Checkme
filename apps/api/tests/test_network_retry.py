"""A turn the network dropped (DNS blip, reset, timeout) is retried instead of ending in "Connection error"."""

from __future__ import annotations

import httpx
import openai

from app.agent import deep


def _chained(outer: BaseException, inner: BaseException) -> BaseException:
    try:
        try:
            raise inner
        except BaseException as exc:
            raise outer from exc
    except BaseException as exc:  # noqa: BLE001
        return exc


def test_dns_and_connection_errors_are_transient():
    req = httpx.Request("POST", "https://api.openai.com/v1/responses")
    assert deep._is_transient_network(openai.APIConnectionError(request=req))
    assert deep._is_transient_network(openai.APITimeoutError(request=req))
    dns = httpx.ConnectError("[Errno -2] Name or service not known")
    assert deep._is_transient_network(dns)
    # LangChain wraps the OpenAI error, which wraps httpx: found anywhere in the chain.
    assert deep._is_transient_network(_chained(RuntimeError("Connection error."), dns))


def test_real_failures_are_not_retried():
    req = httpx.Request("POST", "https://api.openai.com/v1/responses")
    resp = httpx.Response(400, request=req)
    assert not deep._is_transient_network(openai.BadRequestError("bad", response=resp, body=None))
    assert not deep._is_transient_network(ValueError("tool failed"))
    assert not deep._is_transient_network(RuntimeError("recursion limit of 80 reached"))


def test_retry_waits_grow():
    assert len(deep.NETWORK_RETRY_DELAYS) == 2
    assert deep.NETWORK_RETRY_DELAYS[0] < deep.NETWORK_RETRY_DELAYS[1]


def test_langmem_model_sends_reasoning_and_no_temperature(monkeypatch):
    """gpt-6-luna rejects temperature without a reasoning setting (400), which sent every turn to the fallback."""
    import langmem

    from app.config import get_settings
    from app.memory_engine import episodes

    # CI has no OpenAI key; building the client only needs a non-empty one (nothing is sent).
    monkeypatch.setattr(get_settings(), "openai_api_key", "sk-test-not-a-real-key")
    captured = {}

    def fake_manager(model, **kwargs):
        captured["model"] = model
        return object()

    monkeypatch.setattr(langmem, "create_memory_manager", fake_manager)
    episodes._manager()
    model = captured["model"]
    assert model.temperature is None
    assert model.use_responses_api is True
    assert model.reasoning == {"effort": "none"} or model.reasoning
    from app.llm import small_model

    assert model.model_name == small_model()
