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


def search_providers() -> list[tuple[str, Any]]:
    """Configured API providers first (in a fixed order), then keyless engines that work from a server."""
    s = get_settings()
    chain: list[tuple[str, Any]] = []
    for name, key, fn in (
        ("tavily", s.tavily_api_key, _search_tavily),
        ("brave_api", s.brave_search_api_key, _search_brave_api),
        ("serper", s.serper_api_key, _search_serper),
        ("exa", s.exa_api_key, _search_exa),
    ):
        if (key or "").strip():
            chain.append((name, (lambda f, k: (lambda q, n: f(q, n, k)))(fn, key.strip())))
    chain.append(("brave", _search_brave_html))
    chain.append(("duckduckgo", _search_ddg))
    chain.append(("wikipedia", _search_wikipedia))
    return chain


async def web_search(query: str, count: int = 8) -> dict:
    """Real results from the first provider that answers, or a clear error. Never invents results."""
    q = (query or "").strip()
    if not q:
        return {"error": "query is required", "results": []}
    limit = max(1, min(int(count or 8), 12))
    tried: list[str] = []
    for name, search in search_providers():
        try:
            hits = await search(q, limit)
        except SearchBlocked as exc:
            log.warning("web_search %s blocked: %s", name, exc)
            tried.append(f"{name}: blocked ({str(exc)[:120]})")
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning("web_search %s failed: %s: %s", name, type(exc).__name__, str(exc)[:200])
            tried.append(f"{name}: {type(exc).__name__} {str(exc)[:120]}".strip())
            continue
        hits = [h for h in hits if isinstance(h, dict) and str(h.get("url") or "").startswith("http")]
        if not hits:
            tried.append(f"{name}: no results")
            continue
        out: dict[str, Any] = {"query": q, "results": hits[:limit], "source": name}
        if tried:
            out["fallback_from"] = tried
        if name == "wikipedia":
            out["note"] = "Only Wikipedia answered; general web engines were unavailable. Say so if the answer needs fresher sources."
        return out
    log.warning("web_search found nothing for %r: %s", q[:80], "; ".join(tried))
    return {
        "query": q,
        "results": [],
        "error": "Web search returned no results. Engines tried: " + "; ".join(tried)
        + ". Do not invent results: try a different query, open a known site with browser_navigate, or tell the user.",
        "tried": tried,
    }


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
