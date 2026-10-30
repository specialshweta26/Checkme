"""Escalation ladder order, per-step timeouts, blocked reports, Tavily cache, proxy parsing, daemon config."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from app.connectors import browsing, computer_daemon

GOOD = "A real page about bike prices, specs and dealer offers. " * 20
DENIED = {"ok": False, "url": "https://site.example/", "title": "Access Denied", "text": "Access Denied", "status": 403}


def _good(via):
    async def step(url, limit, state):
        return {"ok": True, "url": url, "title": "Bikes", "text": GOOD, "html": "<p>x</p>", "status": 200, "fetched_via": via}

    return step


def _calls(log, name, result):
    async def step(url, limit, state):
        log.append(name)
        return result(url) if callable(result) else result

    return step


async def test_ladder_stops_at_first_usable_step_in_order():
    log: list[str] = []
    steps = {
        "server": _calls(log, "server", dict(DENIED)),
        "proxy": _calls(log, "proxy", None),  # not configured: skipped, not reported
        "tavily": _calls(log, "tavily", {"ok": True, "url": "u", "title": "", "text": "Loading...", "status": 200}),
        "reader": _calls(log, "reader", lambda u: {"ok": True, "url": u, "title": "Bikes", "text": GOOD, "status": 200, "fetched_via": "reader"}),
        "archive": _calls(log, "archive", None),
    }
    out = await browsing.fetch_with_ladder("https://site.example/", ("server", "proxy", "tavily", "reader", "archive"), custom=steps)
    assert log == ["server", "proxy", "tavily", "reader"]
    assert out["ok"] is True and out["fetched_via"] == "reader"
    assert [a["step"] for a in out["attempts"]] == ["server", "tavily", "reader"]
    assert out["attempts"][0]["kind"] == "denied" and out["attempts"][1]["kind"] == "empty_shell"
    assert "note" in out


async def test_ladder_reports_blocked_source_plainly():
    blocked = {s: (lambda s: _calls([], s, dict(DENIED)))(s) for s in ("server", "tavily", "reader")}
    out = await browsing.fetch_with_ladder("https://www.site.example/p", ("server", "tavily", "reader"), custom=blocked)
    assert out["ok"] is False and out["blocked"] is True and out["blocked_source"] == "site.example"
    assert "site.example" in out["error"] and "server (denied)" in out["error"]
    assert "restricted" in out["hint"] and "other sites" in out["hint"]


async def test_each_step_has_a_timeout_and_the_ladder_a_total():
    async def hang(url, limit, state):
        await asyncio.sleep(10)

    started = time.monotonic()
    out = await browsing.fetch_with_ladder(
        "https://slow.example/", ("server", "tavily", "reader"), custom={"server": hang, "tavily": hang, "reader": _good("reader")},
        step_s=0.2, total_s=5,
    )
    assert out["ok"] and out["fetched_via"] == "reader"
    assert [a["error"] for a in out["attempts"][:2]] == ["timed out after 0s", "timed out after 0s"]
    assert time.monotonic() - started < 2

    started = time.monotonic()
    out = await browsing.fetch_with_ladder(
        "https://slow.example/", ("server", "tavily", "reader"), custom={"server": hang, "tavily": hang, "reader": hang},
        step_s=0.4, total_s=0.9,
    )
    assert out["ok"] is False and time.monotonic() - started < 1.5
    assert any("time limit" in a.get("error", "") for a in out["attempts"])


async def test_a_crashing_step_does_not_stop_the_ladder():
    async def boom(url, limit, state):
        raise RuntimeError("proxy http://user:secretpw@p.example:8000 refused")

    out = await browsing.fetch_with_ladder("https://x.example/", ("server", "reader"), custom={"server": boom, "reader": _good("reader")})
    assert out["ok"] and out["attempts"][0]["error"].startswith("RuntimeError")


def test_default_ladders_put_cheap_steps_first():
    assert browsing.LADDER_READ[:4] == ("cache", "server", "proxy", "tavily")
    assert browsing.LADDER_READ[-1] == "archive"
    assert "server" not in browsing.LADDER_AFTER_BROWSER  # the browser already tried the site directly
    assert browsing.LADDER_RESEARCH == ("cache", "server", "proxy", "tavily")


async def test_tavily_raw_content_feeds_the_cache():
    browsing.cache_put("https://www.bikewale.com/x/", "Classic 350", GOOD)
    hit = browsing.cache_get("https://bikewale.com/x")
    assert hit and hit["fetched_via"] == "tavily_cache" and hit["text"].startswith("A real page")
    browsing.cache_put("https://blocked.example/", "Access Denied", "Access Denied")
    assert browsing.cache_get("https://blocked.example/") is None
    out = await browsing.fetch_with_ladder("https://www.bikewale.com/x", browsing.LADDER_READ)
    assert out["fetched_via"] == "tavily_cache" and [a["step"] for a in out["attempts"]] == ["cache"]


def test_proxy_parsing_and_redaction(monkeypatch):
    p = browsing.parse_proxy("http://us%40r:p%3Ass@proxy.example:8080")
    assert p == {"server": "http://proxy.example:8080", "username": "us@r", "password": "p:ss"}
    assert browsing.parse_proxy("") is None and browsing.parse_proxy("ftp://x") is None
    assert browsing.parse_proxy("host.example:3128")["server"] == "http://host.example:3128"
    cfg = SimpleNamespace(browser_proxy_url="http://u:topsecret@proxy.example:8080")
    monkeypatch.setattr(browsing, "get_settings", lambda: cfg)
    assert "topsecret" not in browsing.redact("failed http://u:topsecret@proxy.example:8080 and topsecret")
    assert "abc" not in browsing.redact("wss://connect.example?apiKey=abc&x=1")


def test_alternates_are_the_same_page():
    html = '<link rel="amphtml" href="https://www.site.example/amp/p">'
    alts = browsing.alternate_urls("https://www.site.example/p?a=1", html)
    assert alts[0] == ("amp", "https://www.site.example/amp/p", True)
    assert alts[1] == ("mobile", "https://m.site.example/p?a=1", True)
    assert browsing.alternate_urls("https://m.site.example/p") == []


def test_daemon_config_and_launch_options(monkeypatch):
    cfg = SimpleNamespace(
        browser_locale="en-IN", browser_timezone="", browser_http2=True, browser_challenge_wait_s=15.0,
        browser_channel="", browser_proxy_url="http://u:pw@proxy.example:8080",
    )
    monkeypatch.setattr(browsing, "get_settings", lambda: cfg)
    daemon = browsing.daemon_config("Asia/Kolkata")
    assert daemon["timezone"] == "Asia/Kolkata" and daemon["locale"] == "en-IN" and daemon["proxy"]["password"] == "pw"
    assert len(daemon["hash"]) == 12 and "pw" not in daemon["hash"]
    assert browsing.daemon_config("UTC")["timezone"] == "Asia/Kolkata"

    opts = computer_daemon.launch_options(True, daemon)
    assert opts["ignore_default_args"] == ["--enable-automation"]
    assert "user_agent" not in opts  # headed: the browser's own UA, matching its client hints
    assert "--disable-http2" not in opts["args"] and opts["locale"] == "en-IN" and opts["timezone_id"] == "Asia/Kolkata"
    assert opts["proxy"] == {"server": "http://proxy.example:8080", "username": "u", "password": "pw"}
    headless = computer_daemon.launch_options(False, {"http2": False}, "131.0.6778.33")
    assert "HeadlessChrome" not in headless["user_agent"] and "Chrome/131.0.0.0" in headless["user_agent"]
    assert "--disable-http2" in headless["args"]


def test_daemon_restarts_when_browser_settings_change():
    from app.connectors.daytona_box import daemon_current

    health = 'OK {"ok": true, "version": "%s", "config": "abc123def456"}' % computer_daemon.VERSION
    assert daemon_current(health) and daemon_current(health, "abc123def456")
    assert not daemon_current(health, "000000000000")


def test_browser_profile_cookies_are_trimmed(monkeypatch):
    from app.connectors import browser_profile

    now = 1_000_000.0
    rows = browser_profile.trim_cookies(
        [{"name": "a", "value": "1", "domain": ".x.example", "expires": now + 10}, {"name": "old", "value": "1", "domain": "x", "expires": now - 1}, {"bad": 1}],
        now,
    )
    assert [r["name"] for r in rows] == ["a"]
    assert not browser_profile.enabled("default")  # only real user ids
    big = [{"name": f"c{i}", "value": "v" * 2000, "domain": ".x.example"} for i in range(400)]
    assert len(__import__("json").dumps(browser_profile.trim_cookies(big))) <= browser_profile.MAX_BYTES


@pytest.mark.parametrize("status,expected", [(200, True), (403, False)])
async def test_server_step_uses_the_shared_detector(monkeypatch, status, expected):
    import httpx

    body = "<html><title>Bikes</title><body>" + "<p>" + GOOD + "</p></body></html>" if status == 200 else "<h1>Access Denied</h1>"

    def handler(request):
        return httpx.Response(status, text=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setitem(browsing._clients, "direct", client)
    page = await browsing.http_get("https://bikes.example/")
    assert (not page.get("block")) is expected
    out = await browsing.fetch_with_ladder("https://bikes.example/", ("server",))
    assert out["ok"] is expected
    if not expected:
        assert out["blocked_source"] == "bikes.example"


async def test_blocked_live_browser_falls_back_to_a_read_only_copy(monkeypatch):
    from app.connectors import daytona_box

    sent: list[dict] = []

    async def fake_cmd(proc, payload, timeout=90, fs=None):
        return {"ok": True, "url": "https://www.shop.example/p", "title": "Access Denied", "text": "Access Denied. Reference #18.abc"}

    async def fake_rpc(proc, payload, timeout=90, fs=None):
        sent.append(payload)
        return {"ok": True, "url": payload.get("url"), "title": "", "text": ""}

    monkeypatch.setattr(daytona_box, "_browser_cmd", fake_cmd)
    monkeypatch.setattr(daytona_box, "_browser_rpc", fake_rpc)
    monkeypatch.setitem(browsing._STEPS, "tavily", _good("tavily_extract"))
    out = await daytona_box._open_in_browser(None, None, "https://www.shop.example/p")
    assert out["ok"] is True and out["read_only"] is True and out["fetched_via"] == "tavily_extract"
    assert "bot_wall" not in out and "handoff" not in out and "shop.example" in out["live_site_blocked"]
    assert sent and sent[0]["command"] == "load_html"


async def test_blocked_everywhere_reports_without_handoff_unless_captcha(monkeypatch):
    from app.connectors import daytona_box

    async def denied_cmd(proc, payload, timeout=90, fs=None):
        return {"ok": True, "url": "https://www.shop.example/p", "title": "Access Denied", "text": "Access Denied"}

    monkeypatch.setattr(daytona_box, "_browser_cmd", denied_cmd)
    out = await daytona_box._open_in_browser(None, None, "https://www.shop.example/p")
    assert out["bot_wall"] is True and out["handoff"] is False and out["blocked_source"] == "shop.example"
    assert "other sites" in out["hint"]

    async def captcha_cmd(proc, payload, timeout=90, fs=None):
        return {"ok": True, "url": "https://www.google.com/sorry/index", "title": "Sorry", "text": "unusual traffic"}

    monkeypatch.setattr(daytona_box, "_browser_cmd", captcha_cmd)
    out = await daytona_box._open_in_browser(None, None, "https://www.google.com/search?q=x")
    assert out["bot_wall"] is True and out["handoff"] is True and "take control" in out["hint"]


def test_daemon_runs_every_command_on_one_thread():
    # Playwright's sync API breaks when called from a second thread; the HTTP server uses a thread per request.
    import threading

    from app.connectors import computer_daemon as d

    seen = []
    threads = [threading.Thread(target=lambda: seen.append(d.on_browser_thread(threading.get_ident))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(seen) == 4 and len(set(seen)) == 1
    assert seen[0] not in {t.ident for t in threads}


def test_browser_thread_is_outside_a_running_asyncio_loop():
    # Playwright sync start() raises if get_running_loop() succeeds on that thread.
    import asyncio

    from app.connectors import computer_daemon as d

    def playwright_start_guard():
        d.prepare_thread_for_sync_playwright()
        if d.asyncio_loop_is_running():
            raise RuntimeError(
                "It looks like you are using Playwright Sync API inside the asyncio loop. "
                "Please use the Async API instead."
            )
        return "ok"

    async def from_loop():
        with pytest.raises(RuntimeError, match="asyncio loop"):
            playwright_start_guard()
        return d.on_browser_thread(playwright_start_guard)

    assert asyncio.run(from_loop()) == "ok"
