"""Search fallback into the cloud browser, consent handling, and clear 'configure a key' errors."""

import base64
from types import SimpleNamespace

import httpx
import pytest

from app.agent import web_tools
from app.connectors import computer_daemon as daemon
from app.connectors import daytona_box
from app.connectors.web_fetch import needs_real_browser


def _settings(**keys):
    base = {"tavily_api_key": "", "brave_search_api_key": "", "serper_api_key": "", "exa_api_key": ""}
    base.update(keys)
    return SimpleNamespace(**base)


@pytest.fixture
def blocked_http(monkeypatch):
    """Every keyless engine blocks us, the way Render's IPs saw it on Oct 28."""

    def handler(request: httpx.Request):
        host = request.url.host
        if host == "search.brave.com":
            return httpx.Response(429, text="captcha")
        if "duckduckgo" in host:
            return httpx.Response(202, text='<div class="anomaly-modal__title">bots</div>')
        if host == "en.wikipedia.org":
            return httpx.Response(200, json={"query": {"search": []}})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(web_tools, "_search_client", lambda: client)
    monkeypatch.setattr(web_tools, "_RETRY_DELAY", 0.0)
    monkeypatch.setattr(web_tools, "get_settings", lambda: _settings())


async def test_no_key_and_no_computer_names_the_keys_to_configure(blocked_http, monkeypatch):
    monkeypatch.setattr(web_tools, "computer_slug", lambda ctx: "")
    out = await web_tools.web_search("scooter rental Anjuna", 5, ctx=SimpleNamespace())
    assert out["results"] == []
    assert out["search_api_configured"] is False
    assert out["configure_one_of"] == ["TAVILY_API_KEY", "BRAVE_SEARCH_API_KEY", "SERPER_API_KEY", "EXA_API_KEY"]
    assert "TAVILY_API_KEY" in out["error"] and "brave: blocked" in out["error"]
    assert "duckduckgo: blocked" in out["error"]


async def test_tries_browser_search_before_free_engines(blocked_http, monkeypatch):
    calls = []

    async def fake_call(ctx, slug, tool, args):
        calls.append((slug, tool, args))
        return {"ok": True, "results": [{"title": "Anjuna Bike Rentals", "url": "https://example.com/anjuna"}], "source": "browser:bing"}

    import app.agent.deep as deep

    monkeypatch.setattr(web_tools, "computer_slug", lambda ctx: "computer")
    monkeypatch.setattr(deep, "call_user_tool", fake_call)
    out = await web_tools.web_search("scooter rental Anjuna", 5, ctx=SimpleNamespace())
    assert out["source"] == "browser:bing"
    assert out["results"][0]["url"] == "https://example.com/anjuna"
    assert calls == [("computer", "browser_search", {"query": "scooter rental Anjuna", "count": 5})]
    assert out.get("fallback_from", []) == []


async def test_browser_search_failure_is_listed_in_tried(blocked_http, monkeypatch):
    async def fake_call(ctx, slug, tool, args):
        return {"ok": False, "results": [], "tried": ["browser:bing: blocked (captcha page; not bypassed)"]}

    import app.agent.deep as deep

    monkeypatch.setattr(web_tools, "computer_slug", lambda ctx: "computer")
    monkeypatch.setattr(deep, "call_user_tool", fake_call)
    out = await web_tools.web_search("q", 3, ctx=SimpleNamespace())
    assert out["results"] == []
    assert "browser:bing: blocked (captcha page; not bypassed)" in out["tried"]
    assert "TAVILY_API_KEY" in out["setup"]


async def test_tavily_runs_before_browser(blocked_http, monkeypatch):
    order = []

    async def tavily(q, n, key):
        order.append("tavily")
        return []

    async def fake_call(ctx, slug, tool, args):
        order.append("browser")
        return {"ok": True, "results": [{"title": "browser hit", "url": "https://example.com"}], "source": "browser:bing"}

    import app.agent.deep as deep

    monkeypatch.setattr(web_tools, "get_settings", lambda: _settings(tavily_api_key="x"))
    monkeypatch.setattr(web_tools, "_search_tavily", tavily)
    monkeypatch.setattr(web_tools, "computer_slug", lambda ctx: "computer")
    monkeypatch.setattr(web_tools, "computer_warm", lambda ctx: True)
    monkeypatch.setattr(deep, "call_user_tool", fake_call)
    out = await web_tools.web_search("q", 3, ctx=SimpleNamespace())
    assert order == ["tavily", "browser"]
    assert out["source"] == "browser:bing" and "setup" not in out


async def test_free_success_never_touches_browser_or_paid(monkeypatch):
    async def brave(q, n):
        return [{"title": "Anjuna rentals", "url": "https://example.com/a"}]

    monkeypatch.setattr(web_tools, "get_settings", lambda: _settings())
    monkeypatch.setattr(web_tools, "_search_brave_html", brave)
    out = await web_tools.web_search("anjuna rentals", 3)
    assert out["source"] == "brave"


async def test_tavily_answers_before_a_cold_computer_is_needed(blocked_http, monkeypatch):
    async def tavily(q, n, key):
        return [{"title": "t", "url": "https://example.com"}]

    async def fake_call(*a):
        raise AssertionError("must not boot the computer for a search")

    import app.agent.deep as deep

    monkeypatch.setattr(web_tools, "get_settings", lambda: _settings(tavily_api_key="x"))
    monkeypatch.setattr(web_tools, "_search_tavily", tavily)
    monkeypatch.setattr(web_tools, "computer_slug", lambda ctx: "computer")
    monkeypatch.setattr(web_tools, "computer_warm", lambda ctx: False)
    monkeypatch.setattr(deep, "call_user_tool", fake_call)
    out = await web_tools.web_search("q", 3, ctx=SimpleNamespace())
    assert out["source"] == "tavily"
    assert out.get("fallback_from", []) == []


async def test_cold_computer_is_booted_when_nothing_else_is_left(blocked_http, monkeypatch):
    async def fake_call(ctx, slug, tool, args):
        return {"ok": True, "results": [{"title": "q hit", "url": "https://example.com/q"}], "source": "browser:bing"}

    import app.agent.deep as deep

    monkeypatch.setattr(web_tools, "computer_slug", lambda ctx: "computer")
    monkeypatch.setattr(web_tools, "computer_warm", lambda ctx: False)
    monkeypatch.setattr(deep, "call_user_tool", fake_call)
    out = await web_tools.web_search("q hit", 3, ctx=SimpleNamespace())
    assert out["source"] == "browser:bing"


async def test_stage_timeouts_keep_search_fast(monkeypatch):
    import asyncio

    async def slow(q, n):
        await asyncio.sleep(5)
        return []

    async def slow_browser(ctx, slug, tool, args):
        await asyncio.sleep(5)

    async def tavily(q, n, key):
        await asyncio.sleep(5)
        return []

    import app.agent.deep as deep

    cfg = _settings(tavily_api_key="x")
    cfg.search_provider_timeout_s = 0.05
    cfg.search_free_timeout_s = 0.16
    cfg.search_paid_timeout_s = 0.2
    cfg.search_browser_timeout_s = 0.1
    cfg.search_order = "tavily,browser,free"
    monkeypatch.setattr(web_tools, "get_settings", lambda: cfg)
    for name in ("_search_brave_html", "_search_ddg", "_search_wikipedia"):
        monkeypatch.setattr(web_tools, name, slow)
    monkeypatch.setattr(web_tools, "_search_tavily", tavily)
    monkeypatch.setattr(web_tools, "computer_slug", lambda ctx: "computer")
    monkeypatch.setattr(web_tools, "computer_warm", lambda ctx: True)
    monkeypatch.setattr(deep, "call_user_tool", slow_browser)
    out = await web_tools.web_search("q", 3, ctx=SimpleNamespace())
    assert out["results"] == [] and out["took_s"] < 1.0
    tried = out["tried"]
    assert tried[0] == "tavily: timed out"
    assert tried[1] == "browser: timed out after 0.1s"
    assert tried[2] == "brave: timed out" and tried[3] == "duckduckgo: timed out"
    assert tried[4].startswith("wikipedia:") and ("timed out" in tried[4] or "skipped" in tried[4])


def test_search_order_is_configurable(monkeypatch):
    cfg = _settings(tavily_api_key="x", serper_api_key="y")
    monkeypatch.setattr(web_tools, "get_settings", lambda: cfg)
    assert web_tools.search_order() == ["tavily", "browser", "brave", "duckduckgo", "wikipedia"]
    cfg.search_order = "paid, free"
    assert web_tools.search_order()[:2] == ["tavily", "brave_api"] and "browser" not in web_tools.search_order()
    cfg.search_order = "duckduckgo,browser,serper,bogus"
    assert web_tools.search_order() == ["duckduckgo", "browser", "serper"]
    assert [n for n, _ in web_tools.search_providers()] == ["duckduckgo", "serper"]


def test_unwrap_result_urls():
    target = "https://example.com/a?b=1"
    bing_u = "a1" + base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    assert daytona_box.unwrap_result_url(f"https://www.bing.com/ck/a?!&&p=x&u={bing_u}&ntb=1") == target
    assert daytona_box.unwrap_result_url("https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%3Fb%3D1") == target
    assert daytona_box.unwrap_result_url("https://www.google.com/url?q=https://example.com/a?b=1") == "https://example.com/a?b=1"
    assert daytona_box.unwrap_result_url(target) == target


def test_clean_serp_drops_engine_links_and_dupes():
    rows = [
        {"title": "A", "url": "https://example.com/a"},
        {"title": "A again", "url": "https://example.com/a"},
        {"title": "Images", "url": "https://www.bing.com/images/search?q=x"},
        {"title": "Map", "url": "https://www.google.com/maps/place/x"},
        {"title": "bad", "url": "javascript:void(0)"},
    ]
    out = daytona_box._clean_serp(rows, 8)
    assert [r["url"] for r in out] == ["https://example.com/a", "https://www.google.com/maps/place/x"]


async def test_browser_search_tries_next_engine_after_a_verification_page(monkeypatch):
    seen = []

    async def rpc(proc, payload, timeout, fs):
        seen.append(payload["engine"])
        if payload["engine"] == "bing":
            return {"ok": False, "engine": "bing", "blocked": "captcha"}
        return {"ok": True, "engine": payload["engine"], "results": [{"title": "Goa scooter hire", "url": "https://example.org/r"}]}

    monkeypatch.setattr(daytona_box, "_browser_rpc", rpc)
    out = await daytona_box.browser_search(None, None, "goa scooter", 5)
    assert seen == ["bing", "duckduckgo"]
    assert out["source"] == "browser:duckduckgo"
    assert out["tried"] == ["browser:bing: blocked (captcha page; not bypassed)"]


async def test_browser_search_reports_all_failures(monkeypatch):
    async def rpc(proc, payload, timeout, fs):
        if payload["engine"] == "google":
            raise RuntimeError("timeout")
        return {"ok": True, "results": []}

    monkeypatch.setattr(daytona_box, "_browser_rpc", rpc)
    out = await daytona_box.browser_search(None, None, "q", 5)
    assert out["ok"] is False
    assert out["tried"][0] == "browser:bing: no results"
    assert out["tried"][2].startswith("browser:google: RuntimeError")


def test_js_app_shells_need_the_real_browser():
    # What the server got for Google Maps / Google Search: empty or "click here if not redirected".
    assert needs_real_browser({"ok": True, "url": "https://www.google.com/maps/search/x", "title": "Google Maps", "text": ""})
    assert needs_real_browser({"ok": False, "status": 403, "text": "Forbidden"})
    assert needs_real_browser({"ok": True, "text": "Please click here if you are not redirected within a few seconds."})
    assert not needs_real_browser({"ok": True, "text": "A long article. " * 40})


def test_consent_detection_is_not_a_bot_wall():
    assert daemon.looks_like_consent("https://consent.google.com/ml?continue=x", "Before you continue to Google")
    assert daemon.looks_like_consent("https://example.com", "We use cookies to improve your experience. Accept all")
    assert not daemon.looks_like_consent("https://example.com", "Our systems have detected unusual traffic. We use cookies.")
    assert not daemon.looks_like_consent("https://example.com", "Plain article text about Goa.")
    assert daemon._CONSENT_REJECT.match("Reject all") and daemon._CONSENT_ACCEPT.match("Accept all")
    assert not daemon._CONSENT_ACCEPT.match("I'm not a robot")


def test_clean_serp_drops_unrelated_filler_results():
    rows = [
        {"title": "Speedtest by Ookla", "url": "https://www.speedtest.net/"},
        {"title": "Anjuna scooter hire", "url": "https://example.com/hire"},
    ]
    out = daytona_box._clean_serp(rows, 8, "scooter rental Anjuna Goa")
    assert [r["url"] for r in out] == ["https://example.com/hire"]
