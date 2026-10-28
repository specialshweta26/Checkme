"""Public web search/fetch and optional media generation."""

from __future__ import annotations

import html as html_lib
import logging
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from connector_core.http import shared_client

from app.agent.computer_harness import computer_slug, run_computer
from app.config import get_settings

log = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
_RESULT_A = re.compile(
    r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
    re.I | re.S,
)
_SNIPPET = re.compile(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)<', re.I | re.S)
_TAG = re.compile(r"<[^>]+>")


def _plain(value: str) -> str:
    return _TAG.sub("", html_lib.unescape(value or "")).strip()


def unwrap_ddg(url: str) -> str:
    raw = html_lib.unescape(url or "").strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    parsed = urlparse(raw)
    if "duckduckgo.com" in (parsed.netloc or "") and parsed.path.startswith("/l"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            return unquote(target)
    return raw


def parse_ddg_html(body: str, limit: int = 8) -> list[dict]:
    hits: list[dict] = []
    snippets = [_plain(s) for s in _SNIPPET.findall(body or "")]
    for i, (href, title) in enumerate(_RESULT_A.findall(body or "")):
        url = unwrap_ddg(href)
        if not url.startswith("http"):
            continue
        hits.append(
            {
                "title": _plain(title)[:200] or url,
                "url": url,
                "snippet": snippets[i][:400] if i < len(snippets) else "",
            }
        )
        if len(hits) >= limit:
            break
    return hits


_BRAVE_SPLIT = re.compile(r'data-type="web"', re.I)
_BRAVE_LINK = re.compile(r'<a[^>]*href="(https?://[^"]+)"', re.I)
_BRAVE_TITLE = re.compile(r'class="[^"]*search-snippet-title[^"]*"[^>]*title="([^"]*)"', re.I)
_BRAVE_SNIPPET = re.compile(r'class="(?:[^"]*\s)?(?:content|snippet-description)(?:\s[^"]*)?"[^>]*>(.*?)</div>', re.I | re.S)
_SEARCH_TIMEOUT = 10.0
_RETRY_DELAY = 0.6
_BLOCK_MARKERS = ("anomaly-modal", "anomaly.js", "challenge-form", "captcha", "unusual traffic", "are you a robot")


class SearchBlocked(Exception):
    """The engine answered with a bot check / rate limit instead of results."""


def parse_brave_html(body: str, limit: int = 8) -> list[dict]:
    """Organic results from search.brave.com HTML (each result block carries data-type="web")."""
    hits: list[dict] = []
    seen: set[str] = set()
    for block in _BRAVE_SPLIT.split(body or "")[1:]:
        block = block[:12000]
        link = _BRAVE_LINK.search(block)
        if not link:
            continue
        url = html_lib.unescape(link.group(1))
        host = (urlparse(url).hostname or "").lower()
        if not host or host.endswith("brave.com") or url in seen:
            continue
        title_m = _BRAVE_TITLE.search(block)
        snip_m = _BRAVE_SNIPPET.search(block)
        title = _plain(title_m.group(1)) if title_m else ""
        snippet = re.sub(r"\s+", " ", _plain(snip_m.group(1))) if snip_m else ""
        seen.add(url)
        hits.append({"title": title[:200] or url, "url": url, "snippet": snippet[:400]})
        if len(hits) >= limit:
            break
    return hits


def _looks_blocked(status: int, body: str) -> bool:
    if status in {202, 403, 429}:
        return True
    low = (body or "")[:200000].lower()
    return any(marker in low for marker in _BLOCK_MARKERS)


_SEARCH_CLIENT = None


def _search_client():
    """HTTP/2 client for search engines: several (Brave included) rate-limit HTTP/1.1 clients as bots."""
    global _SEARCH_CLIENT
    if _SEARCH_CLIENT is None:
        import httpx

        try:
            import h2  # noqa: F401

            _SEARCH_CLIENT = httpx.AsyncClient(http2=True, timeout=_SEARCH_TIMEOUT)
        except ImportError:
            _SEARCH_CLIENT = shared_client()
    return _SEARCH_CLIENT


async def _request(method: str, url: str, *, attempts: int = 2, **kwargs):
    """One HTTP call with a short timeout and one retry on timeouts, connection errors and 5xx."""
    import asyncio

    import httpx

    last: Exception | None = None
    timeout = kwargs.pop("timeout", _SEARCH_TIMEOUT)
    for attempt in range(attempts):
        try:
            resp = await _search_client().request(method, url, timeout=timeout, **kwargs)
            if resp.status_code >= 500 and attempt + 1 < attempts:
                last = RuntimeError(f"HTTP {resp.status_code}")
                await asyncio.sleep(_RETRY_DELAY * (attempt + 1))
                continue
            return resp
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(_RETRY_DELAY * (attempt + 1))
    raise last or RuntimeError("request failed")


def _http_error(resp) -> str:
    return f"HTTP {resp.status_code}: {(resp.text or '')[:160]}"


async def _search_tavily(q: str, limit: int, key: str) -> list[dict]:
    resp = await _request(
        "POST",
        "https://api.tavily.com/search",
        json={"query": q, "max_results": limit, "search_depth": "basic"},
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout=15.0,
    )
    if resp.status_code >= 400:
        raise RuntimeError(_http_error(resp))
    data = resp.json() or {}
    return [
        {"title": str(r.get("title") or r.get("url") or "")[:200], "url": r.get("url") or "", "snippet": str(r.get("content") or "")[:400]}
        for r in data.get("results") or []
        if isinstance(r, dict) and r.get("url")
    ]


async def _search_brave_api(q: str, limit: int, key: str) -> list[dict]:
    resp = await _request(
        "GET",
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": q, "count": limit},
        headers={"X-Subscription-Token": key, "Accept": "application/json"},
    )
    if resp.status_code >= 400:
        raise RuntimeError(_http_error(resp))
    rows = ((resp.json() or {}).get("web") or {}).get("results") or []
    return [
        {"title": _plain(str(r.get("title") or ""))[:200] or r.get("url"), "url": r.get("url") or "", "snippet": _plain(str(r.get("description") or ""))[:400]}
        for r in rows
        if isinstance(r, dict) and r.get("url")
    ]


async def _search_serper(q: str, limit: int, key: str) -> list[dict]:
    resp = await _request(
        "POST",
        "https://google.serper.dev/search",
        json={"q": q, "num": limit},
        headers={"X-API-KEY": key, "Content-Type": "application/json"},
    )
    if resp.status_code >= 400:
        raise RuntimeError(_http_error(resp))
    rows = (resp.json() or {}).get("organic") or []
    return [
        {"title": str(r.get("title") or "")[:200] or r.get("link"), "url": r.get("link") or "", "snippet": str(r.get("snippet") or "")[:400]}
        for r in rows
        if isinstance(r, dict) and r.get("link")
    ]


async def _search_exa(q: str, limit: int, key: str) -> list[dict]:
    resp = await _request(
        "POST",
        "https://api.exa.ai/search",
        json={"query": q, "numResults": limit, "contents": {"text": {"maxCharacters": 400}}},
        headers={"x-api-key": key, "Content-Type": "application/json"},
        timeout=15.0,
    )
    if resp.status_code >= 400:
        raise RuntimeError(_http_error(resp))
    rows = (resp.json() or {}).get("results") or []
    return [
        {"title": str(r.get("title") or r.get("url") or "")[:200], "url": r.get("url") or "", "snippet": str(r.get("text") or "")[:400]}
        for r in rows
        if isinstance(r, dict) and r.get("url")
    ]


async def _search_brave_html(q: str, limit: int) -> list[dict]:
    resp = await _request(
        "GET",
        "https://search.brave.com/search",
        params={"q": q, "source": "web"},
        headers={"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.9"},
        follow_redirects=True,
    )
    body = resp.text or ""
    hits = parse_brave_html(body, limit) if resp.status_code == 200 else []
    if hits:
        return hits
    if _looks_blocked(resp.status_code, body):
        raise SearchBlocked(f"search.brave.com answered {resp.status_code} with a bot check")
    if resp.status_code >= 400:
        raise RuntimeError(_http_error(resp))
    return []


async def _search_ddg(q: str, limit: int) -> list[dict]:
    errors = []
    for url in ("https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/"):
        resp = await _request(
            "POST",
            url,
            data={"q": q},
            headers={"User-Agent": _UA, "Accept": "text/html", "Accept-Language": "en-US,en;q=0.9"},
            follow_redirects=True,
        )
        body = resp.text or ""
        hits = (parse_ddg_html(body, limit) or parse_ddg_lite(body, limit)) if resp.status_code == 200 else []
        if hits:
            return hits
        # DuckDuckGo answers server IPs with HTTP 202 + an "anomaly" bot challenge instead of results.
        if _looks_blocked(resp.status_code, body):
            errors.append(f"{urlparse(url).hostname} {resp.status_code} bot check")
    if errors:
        raise SearchBlocked("; ".join(errors))
    return []


_LITE_A = re.compile(r"<a[^>]*class=['\"]result-link['\"][^>]*href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>", re.I | re.S)
_LITE_A2 = re.compile(r"<a[^>]*href=['\"]([^'\"]+)['\"][^>]*class=['\"]result-link['\"][^>]*>(.*?)</a>", re.I | re.S)
_LITE_SNIP = re.compile(r"class=['\"]result-snippet['\"][^>]*>(.*?)</td>", re.I | re.S)


def parse_ddg_lite(body: str, limit: int = 8) -> list[dict]:
    links = _LITE_A.findall(body or "") or _LITE_A2.findall(body or "")
    snippets = [_plain(s) for s in _LITE_SNIP.findall(body or "")]
    hits = []
    for i, (href, title) in enumerate(links):
        url = unwrap_ddg(href)
        if not url.startswith("http"):
            continue
        hits.append({"title": _plain(title)[:200] or url, "url": url, "snippet": snippets[i][:400] if i < len(snippets) else ""})
        if len(hits) >= limit:
            break
    return hits


async def _search_wikipedia(q: str, limit: int) -> list[dict]:
    resp = await _request(
        "GET",
        "https://en.wikipedia.org/w/api.php",
        params={"action": "query", "list": "search", "srsearch": q, "format": "json", "srlimit": min(limit, 10)},
        headers={"User-Agent": "CheckmeBot/1.0 (https://github.com/specialshweta26/Checkme; agent web search)", "Accept": "application/json"},
    )
    if resp.status_code >= 400:
        raise RuntimeError(_http_error(resp))
    rows = ((resp.json() or {}).get("query") or {}).get("search") or []
    out = []
    for r in rows:
        title = str(r.get("title") or "")
        if not title:
            continue
        out.append(
            {
                "title": title[:200],
                "url": "https://en.wikipedia.org/wiki/" + title.replace(" ", "_"),
                "snippet": _plain(str(r.get("snippet") or ""))[:400],
            }
        )
    return out


FREE_PROVIDERS = ("brave", "duckduckgo", "wikipedia")
PAID_PROVIDERS = ("tavily", "brave_api", "serper", "exa")  # Tavily first among the paid ones
DEFAULT_SEARCH_ORDER = "free,browser,paid"
SEARCH_KEY_ENVS = ("TAVILY_API_KEY", "BRAVE_SEARCH_API_KEY", "SERPER_API_KEY", "EXA_API_KEY")
_PAID_KEYS = {"tavily": "tavily_api_key", "brave_api": "brave_search_api_key", "serper": "serper_api_key", "exa": "exa_api_key"}
_PAID_FNS = {"tavily": "_search_tavily", "brave_api": "_search_brave_api", "serper": "_search_serper", "exa": "_search_exa"}
_FREE_FNS = {"brave": "_search_brave_html", "duckduckgo": "_search_ddg", "wikipedia": "_search_wikipedia"}
_STOP = frozenset("the and for with from that this what when where which near best how are you your www com".split())


def _cfg(name: str, default: Any) -> Any:
    value = getattr(get_settings(), name, None)
    return default if value in (None, "") else value


def _key(name: str) -> str:
    return str(getattr(get_settings(), _PAID_KEYS[name], "") or "").strip()


def search_order() -> list[str]:
    """SEARCH_ORDER as stages: 'free', 'browser', 'paid', or single provider names (brave, duckduckgo, wikipedia,
    tavily, brave_api, serper, exa). Default 'free,browser,paid': paid APIs are the last resort."""
    raw = str(_cfg("search_order", DEFAULT_SEARCH_ORDER))
    out: list[str] = []
    for token in (t.strip().lower() for t in raw.split(",")):
        names = {"free": FREE_PROVIDERS, "paid": PAID_PROVIDERS, "browser": ("browser",)}.get(token, (token,))
        for name in names:
            if name in FREE_PROVIDERS + PAID_PROVIDERS + ("browser",) and name not in out:
                out.append(name)
    return out or list(FREE_PROVIDERS + ("browser",) + PAID_PROVIDERS)


def search_providers() -> list[tuple[str, Any]]:
    """HTTP providers in SEARCH_ORDER (the browser stage is handled by web_search). Paid ones only when keyed."""
    chain: list[tuple[str, Any]] = []
    for name in search_order():
        if name in _FREE_FNS:
            chain.append((name, globals()[_FREE_FNS[name]]))
        elif name in _PAID_FNS and _key(name):
            fn, key = globals()[_PAID_FNS[name]], _key(name)
            chain.append((name, (lambda f, k: (lambda q, n: f(q, n, k)))(fn, key)))
    return chain


def search_api_configured() -> bool:
    return any(_key(name) for name in PAID_PROVIDERS)


def setup_hint() -> str:
    if search_api_configured():
        return ""
    return (
        "No paid search API key is configured, so when the free engines (Brave, DuckDuckGo, Wikipedia) block or "
        "rate-limit the cloud server and the computer's browser search also fails, there is nothing left to try. "
        "Set TAVILY_API_KEY (or BRAVE_SEARCH_API_KEY, SERPER_API_KEY, EXA_API_KEY) on the API service "
        "(Render > checkme-api > Environment); it is only used as the last resort."
    )


def _query_terms(query: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]{3,}", (query or "").lower()) if t not in _STOP}


def _relevant(hits: list[dict], query: str) -> list[dict]:
    terms = _query_terms(query)
    if not terms:
        return hits
    return [h for h in hits if any(t in f"{h.get('title') or ''} {h.get('snippet') or ''} {h.get('url') or ''}".lower() for t in terms)]


def computer_warm(ctx) -> bool | None:
    """True/False when we can tell whether the cloud computer is already running in this process; None if unknown."""
    slug = computer_slug(ctx) if ctx is not None else ""
    if not slug:
        return None
    try:
        from app.connectors.daytona_box import computer_warm as warm

        return warm(ctx.connectors.get(slug))
    except Exception:  # noqa: BLE001
        return None


async def _browser_search(ctx, query: str, limit: int) -> dict | None:
    """Search inside the cloud computer's real browser. None when no computer is connected."""
    slug = computer_slug(ctx) if ctx is not None else ""
    if not slug:
        return None
    from app.agent.deep import call_user_tool

    try:
        data = await call_user_tool(ctx, slug, "browser_search", {"query": query, "count": limit})
    except Exception as exc:  # noqa: BLE001
        log.warning("web_search browser fallback failed: %s: %s", type(exc).__name__, str(exc)[:200])
        return {"results": [], "tried": [f"browser: {type(exc).__name__} {str(exc)[:120]}".strip()]}
    return data if isinstance(data, dict) else {"results": [], "tried": ["browser: no response"]}


def _browser_plan(ctx, paid_left: bool) -> tuple[bool, float, str]:
    """(run?, timeout seconds, reason if skipped). Cold boots are slow, so by default ('auto') a cold computer is
    only booted for search when no paid provider is left to try."""
    if ctx is None or not computer_slug(ctx):
        return False, 0.0, "browser: skipped (no cloud computer connected)"
    warm = computer_warm(ctx)
    timeout = float(_cfg("search_browser_timeout_s", 45.0))
    if warm is not False:
        return True, timeout, ""
    policy = str(_cfg("search_browser_boot", "auto")).lower()
    if policy == "never" or (policy == "auto" and paid_left):
        return False, 0.0, "browser: skipped (computer is not running; a cold boot is too slow for a search)"
    return True, max(timeout, float(_cfg("search_browser_cold_timeout_s", 120.0))), ""


async def web_search(query: str, count: int = 8, ctx=None) -> dict:
    """Real results from the first stage that answers, or a clear error. Never invents results.

    Default order (SEARCH_ORDER=free,browser,paid): keyless HTTP engines, then the same search in the cloud
    computer's real browser, then paid APIs (Tavily first) as the last resort. Each stage has a time budget.
    Verification pages are reported, never bypassed.
    """
    import asyncio
    import time

    q = (query or "").strip()
    if not q:
        return {"error": "query is required", "results": []}
    limit = max(1, min(int(count or 8), 12))
    tried: list[str] = []
    order = search_order()
    providers = dict(search_providers())
    hint = setup_hint()
    per_provider = float(_cfg("search_provider_timeout_s", 8.0))
    budgets = {"free": float(_cfg("search_free_timeout_s", 15.0)), "paid": float(_cfg("search_paid_timeout_s", 20.0))}
    spent = {"free": 0.0, "paid": 0.0}

    def _ok(hits: list, source: str, started: float) -> dict:
        out: dict[str, Any] = {"query": q, "results": hits[:limit], "source": source, "took_s": round(time.monotonic() - started, 1)}
        if tried:
            out["fallback_from"] = list(tried)
        return out

    started = time.monotonic()
    for i, name in enumerate(order):
        if name == "browser":
            paid_left = any(n in PAID_PROVIDERS and n in providers for n in order[i + 1 :])
            run, timeout, why = _browser_plan(ctx, paid_left)
            if not run:
                tried.append(why)
                continue
            try:
                data = await asyncio.wait_for(_browser_search(ctx, q, limit), timeout=timeout)
            except (asyncio.TimeoutError, TimeoutError):
                log.warning("web_search browser stage timed out after %.0fs", timeout)
                tried.append(f"browser: timed out after {timeout:g}s")
                continue
            data = data or {}
            hits = _relevant([h for h in (data.get("results") or []) if isinstance(h, dict) and str(h.get("url") or "").startswith("http")], q)
            if hits:
                log.info("web_search answered by %s after: %s", data.get("source") or "browser", "; ".join(tried))
                out = _ok(hits, str(data.get("source") or "browser"), started)
                out["note"] = "Results were read from a search page in the cloud browser; open them to verify."
                return out
            tried.extend(str(t) for t in (data.get("tried") or ["browser: no results"]))
            continue
        search = providers.get(name)
        if search is None:
            continue  # a paid provider without a key
        stage = "paid" if name in PAID_PROVIDERS else "free"
        left = budgets[stage] - spent[stage]
        if left <= 0.05:
            tried.append(f"{name}: skipped ({stage} search time budget of {budgets[stage]:g}s used up)")
            continue
        t0 = time.monotonic()
        try:
            hits = await asyncio.wait_for(search(q, limit), timeout=min(per_provider if stage == "free" else left, left))
        except (asyncio.TimeoutError, TimeoutError):
            log.warning("web_search %s timed out", name)
            tried.append(f"{name}: timed out")
            continue
        except SearchBlocked as exc:
            log.warning("web_search %s blocked: %s", name, exc)
            tried.append(f"{name}: blocked ({str(exc)[:120]})")
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning("web_search %s failed: %s: %s", name, type(exc).__name__, str(exc)[:200])
            tried.append(f"{name}: {type(exc).__name__} {str(exc)[:120]}".strip())
            continue
        finally:
            spent[stage] += time.monotonic() - t0
        hits = [h for h in hits if isinstance(h, dict) and str(h.get("url") or "").startswith("http")]
        if name == "wikipedia":
            hits = _relevant(hits, q)
        if not hits:
            tried.append(f"{name}: no results")
            continue
        out = _ok(hits, name, started)
        if name == "wikipedia":
            out["note"] = "Only Wikipedia answered; general web engines were unavailable. Say so if the answer needs fresher sources."
        return out
    log.warning("web_search found nothing for %r: %s", q[:80], "; ".join(tried))
    error = "Web search returned no results. Engines tried, in order: " + "; ".join(tried) + "."
    if hint:
        error += " " + hint
    error += (
        " Do not invent results: open a known site directly with browser_navigate, and tell the user plainly "
        "which search engines failed" + (" and which key to configure." if hint else ".")
    )
    out: dict[str, Any] = {
        "query": q,
        "results": [],
        "error": error,
        "tried": tried,
        "order": order,
        "search_api_configured": not hint,
        "took_s": round(time.monotonic() - started, 1),
    }
    if hint:
        out["setup"] = hint
        out["configure_one_of"] = list(SEARCH_KEY_ENVS)
    return out


async def web_fetch(ctx, url: str) -> Any:
    target = (url or "").strip()
    if not target:
        return {"error": "url is required"}
    if computer_slug(ctx):
        try:
            painted = await run_computer(ctx, "web_fetch", {"url": target})
        except Exception as exc:  # noqa: BLE001
            log.warning("web_fetch on the computer failed: %s", type(exc).__name__)
            painted = {"error": str(exc)[:200]}
        if not (isinstance(painted, dict) and painted.get("error")):
            return painted
    from app.connectors.web_fetch import fetch_public_page

    try:
        return await fetch_public_page(target)
    except Exception as exc:  # noqa: BLE001 - a slow or dead site must not fail the whole chat turn
        log.warning("web_fetch %s failed: %s", target[:200], type(exc).__name__)
        return {
            "ok": False,
            "url": target,
            "error": f"Could not fetch the page ({type(exc).__name__}: {str(exc)[:160]}). Try another source; do not invent its content.",
        }


async def generate_image(prompt: str, size: str = "1024x1024") -> dict:
    text = (prompt or "").strip()
    if not text:
        return {"error": "prompt is required"}
    if not get_settings().openai_api_key:
        return {"error": "Image generation is not configured (missing OPENAI_API_KEY)."}
    dim = size if size in {"1024x1024", "1024x1792", "1792x1024"} else "1024x1024"
    try:
        from app.llm import openai_client

        resp = await openai_client().images.generate(model="dall-e-3", prompt=text[:4000], size=dim)
        row = (resp.data or [None])[0]
        url = getattr(row, "url", None) if row is not None else None
        if not url:
            return {"error": "The image API returned no URL."}
        return {"ok": True, "url": url, "prompt": text, "note": "OpenAI image URLs expire; download soon."}
    except Exception as exc:  # noqa: BLE001
        log.warning("generate_image failed: %s", exc)
        return {"error": str(exc)[:300]}


def generate_video(prompt: str = "") -> dict:
    return {
        "error": "Video generation is not configured. Connect a video provider first.",
        "prompt": (prompt or "")[:200],
    }
