"""web_search provider chain with mocked HTTP: real results or a clear error, never invented ones."""

import json
from types import SimpleNamespace

import httpx
import pytest

from app.agent import web_tools

BRAVE_HTML = """
<html><body>
<div class="snippet" data-type="web" data-pos="1"><a href="https://www.accuweather.com/en/in/pune/weather" class="l1">
<div class="title search-snippet-title line-clamp-1" title="Pune Weather Forecast &amp; More">Pune Weather</div></a>
<div class="generic-snippet"><div class="content desktop-default-regular t-primary"><span>2 days ago -</span> Sunny, high of <strong>32°C</strong>.</div> <!--x--></div></div>
<div class="snippet" data-type="web" data-pos="2"><a href="https://en.wikipedia.org/wiki/Pune" class="l1">
<div class="title search-snippet-title" title="Pune - Wikipedia">Pune</div></a>
<div class="generic-snippet"><div class="content">Pune is a city in Maharashtra.</div></div></div>
<div class="snippet" data-type="web"><a href="https://search.brave.com/settings">settings</a></div>
</body></html>
"""

DDG_ANOMALY = '<html><body><div class="anomaly-modal__title">Unfortunately, bots use DuckDuckGo too.</div></body></html>'
DDG_HTML = (
    '<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">Example A</a>'
    '<a class="result__snippet" href="x">About A</a>'
)


def _settings(**keys):
    base = {"tavily_api_key": "", "brave_search_api_key": "", "serper_api_key": "", "exa_api_key": ""}
    base.update(keys)
    return SimpleNamespace(**base)


@pytest.fixture
def http(monkeypatch):
    routes = {}
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        key = f"{request.method} {request.url.host}{request.url.path}"
        fn = routes.get(key)
        if fn is None:
            return httpx.Response(404, text="no route")
        return fn(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(web_tools, "_search_client", lambda: client)
    monkeypatch.setattr(web_tools, "_RETRY_DELAY", 0.0)
    monkeypatch.setattr(web_tools, "get_settings", lambda: _settings())
    return SimpleNamespace(routes=routes, seen=seen)


def test_parse_brave_html():
    hits = web_tools.parse_brave_html(BRAVE_HTML)
    assert [h["url"] for h in hits] == ["https://www.accuweather.com/en/in/pune/weather", "https://en.wikipedia.org/wiki/Pune"]
    assert hits[0]["title"] == "Pune Weather Forecast & More"
    assert "Sunny, high of 32°C" in hits[0]["snippet"]


def test_parse_ddg_lite():
    body = (
        "<a rel='nofollow' href='//duckduckgo.com/l/?uddg=https%3A%2F%2Fex.org%2F' class='result-link'>Ex Org</a>"
        "<td class='result-snippet'>Snippet <b>one</b></td>"
    )
    assert web_tools.parse_ddg_lite(body) == [{"title": "Ex Org", "url": "https://ex.org/", "snippet": "Snippet one"}]


async def test_keyless_brave_results(http):
    http.routes["GET search.brave.com/search"] = lambda r: httpx.Response(200, text=BRAVE_HTML)
    out = await web_tools.web_search("pune weather")
    assert out["source"] == "brave"
    assert out["results"][0]["url"].startswith("https://www.accuweather.com/")
    assert "error" not in out


async def test_ddg_202_challenge_is_detected_and_falls_through(http):
    http.routes["GET search.brave.com/search"] = lambda r: httpx.Response(429, text="Too many requests")
    http.routes["POST html.duckduckgo.com/html/"] = lambda r: httpx.Response(202, text=DDG_ANOMALY)
    http.routes["POST lite.duckduckgo.com/lite/"] = lambda r: httpx.Response(202, text=DDG_ANOMALY)
    http.routes["GET en.wikipedia.org/w/api.php"] = lambda r: httpx.Response(
        200, json={"query": {"search": [{"title": "Pune", "snippet": "<span>Pune</span> is a city"}]}}
    )
    out = await web_tools.web_search("pune")
    assert out["source"] == "wikipedia"
    assert out["results"] == [{"title": "Pune", "url": "https://en.wikipedia.org/wiki/Pune", "snippet": "Pune is a city"}]
    assert any("duckduckgo: blocked" in t and "202" in t for t in out["fallback_from"])
    assert any(t.startswith("brave: blocked") for t in out["fallback_from"])
    assert "note" in out


async def test_ddg_results_when_not_blocked(http):
    http.routes["GET search.brave.com/search"] = lambda r: httpx.Response(200, text="<html>nothing</html>")
    http.routes["POST html.duckduckgo.com/html/"] = lambda r: httpx.Response(200, text=DDG_HTML)
    out = await web_tools.web_search("example")
    assert out["source"] == "duckduckgo"
    assert out["results"][0] == {"title": "Example A", "url": "https://example.com/a", "snippet": "About A"}


async def test_all_engines_fail_returns_clear_error_not_invented_results(http):
    def boom(request):
        raise httpx.ReadTimeout("timed out", request=request)

    for key in ("GET search.brave.com/search", "POST html.duckduckgo.com/html/", "POST lite.duckduckgo.com/lite/", "GET en.wikipedia.org/w/api.php"):
        http.routes[key] = boom
    out = await web_tools.web_search("anything")
    assert out["results"] == []
    assert "no results" in out["error"].lower() and "do not invent" in out["error"].lower()
    assert len(out["tried"]) == 3
    # one retry per request on timeouts
    assert sum(1 for r in http.seen if r.url.host == "search.brave.com") == 2


async def test_retry_recovers_from_a_5xx(http):
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        return httpx.Response(503, text="busy") if calls["n"] == 1 else httpx.Response(200, text=BRAVE_HTML)

    http.routes["GET search.brave.com/search"] = flaky
    out = await web_tools.web_search("pune")
    assert out["source"] == "brave" and calls["n"] == 2


async def test_api_providers_first_in_order(http, monkeypatch):
    monkeypatch.setattr(web_tools, "get_settings", lambda: _settings(tavily_api_key="tv", serper_api_key="sp"))
    http.routes["POST api.tavily.com/search"] = lambda r: httpx.Response(401, json={"detail": "bad key"})

    def serper(request):
        assert request.headers["X-API-KEY"] == "sp"
        assert json.loads(request.content)["q"] == "fastapi"
        return httpx.Response(200, json={"organic": [{"title": "FastAPI", "link": "https://fastapi.tiangolo.com/", "snippet": "Docs"}]})

    http.routes["POST google.serper.dev/search"] = serper
    out = await web_tools.web_search("fastapi")
    assert out["source"] == "serper"
    assert out["results"] == [{"title": "FastAPI", "url": "https://fastapi.tiangolo.com/", "snippet": "Docs"}]
    assert out["fallback_from"][0].startswith("tavily: RuntimeError HTTP 401")
    assert [name for name, _ in web_tools.search_providers()][:2] == ["tavily", "serper"]


async def test_tavily_brave_exa_parsers(http, monkeypatch):
    monkeypatch.setattr(web_tools, "get_settings", lambda: _settings(tavily_api_key="tv"))
    http.routes["POST api.tavily.com/search"] = lambda r: httpx.Response(
        200, json={"results": [{"title": "T", "url": "https://t.example/", "content": "tc"}]}
    )
    assert (await web_tools.web_search("q"))["results"][0]["url"] == "https://t.example/"
    monkeypatch.setattr(web_tools, "get_settings", lambda: _settings(brave_search_api_key="bk"))
    http.routes["GET api.search.brave.com/res/v1/web/search"] = lambda r: httpx.Response(
        200, json={"web": {"results": [{"title": "<b>B</b>", "url": "https://b.example/", "description": "bd"}]}}
    )
    out = await web_tools.web_search("q")
    assert out["source"] == "brave_api" and out["results"][0]["title"] == "B"
    monkeypatch.setattr(web_tools, "get_settings", lambda: _settings(exa_api_key="ek"))
    http.routes["POST api.exa.ai/search"] = lambda r: httpx.Response(
        200, json={"results": [{"title": "E", "url": "https://e.example/", "text": "et"}]}
    )
    out = await web_tools.web_search("q")
    assert out["source"] == "exa" and out["results"][0]["snippet"] == "et"


async def test_empty_query_is_an_error():
    assert (await web_tools.web_search("  "))["error"]


async def test_web_fetch_timeout_returns_error_instead_of_crashing(monkeypatch):
    async def slow(url):
        raise httpx.ReadTimeout("read timed out")

    import app.connectors.web_fetch as wf

    monkeypatch.setattr(wf, "fetch_public_page", slow)
    monkeypatch.setattr(web_tools, "computer_slug", lambda ctx: "")
    out = await web_tools.web_fetch(SimpleNamespace(), "https://slow.example/")
    assert out["ok"] is False and "ReadTimeout" in out["error"]
