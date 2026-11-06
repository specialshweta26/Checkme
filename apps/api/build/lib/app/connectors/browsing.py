"""Resilient, read-only page fetching: one escalation ladder for every server-side fetch path.

A site can refuse a request because of *where* it comes from (a datacenter IP), *what* it looks like (headers,
TLS, HTTP version) or *how fast* it arrives. No single trick works everywhere, so each URL walks a short ladder
of independent ways to read it, each with its own time limit, and stops at the first usable page:

    cache     page text Tavily already returned with the search results (instant, no extra request)
    server    direct HTTP fetch from the API with a complete, consistent Chrome header set (en-IN)
    proxy     the same through BROWSER_PROXY_URL (residential/ISP proxy), when configured
    tavily    Tavily Extract (its own crawler network), when TAVILY_API_KEY is set
    remote    a hosted browser (REMOTE_BROWSER_PROVIDER=steel) with its own network and fingerprint
    alternates the site's mobile (m.) or AMP version of the same page
    reader    a reader service (r.jina.ai) that renders the page in its own browser and returns text
    archive   the latest Internet Archive copy (labelled as archived; may be out of date)

Every step's response goes through the shared block detector (``block_detect``), so a CAPTCHA, a bot check, an
"Access Denied" page or an empty JS shell never counts as success. When every step fails the result says so
plainly: ``{"ok": False, "blocked": True, "blocked_source": "<host>", "attempts": [...], "hint": ...}`` so the
agent names the blocked site and moves on to other sources instead of inventing content.

Nothing here solves CAPTCHAs or forges identities; it reads public pages the way other clients can.
See docs/BROWSING.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse, urlunparse

import httpx

from app.config import get_settings
from app.connectors.block_detect import THIN_TEXT, detect_block, host_of, page_is_thin

log = logging.getLogger(__name__)

CHROME_MAJOR = "131"
_UA_DESKTOP = (
    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{CHROME_MAJOR}.0.0.0 Safari/537.36"
)
_UA_MOBILE = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{CHROME_MAJOR}.0.0.0 Mobile Safari/537.36"
)

LADDER_READ = ("cache", "server", "proxy", "tavily", "remote", "alternates", "reader", "archive")
LADDER_AFTER_BROWSER = ("cache", "proxy", "tavily", "remote", "alternates", "reader", "archive")
LADDER_RESEARCH = ("cache", "server", "proxy", "tavily")
# Quick answers: server-side copies only (no hosted/computer browser), each fast; see web_tools.web_fetch.
LADDER_QUICK = ("cache", "server", "proxy", "tavily", "alternates", "reader")


def _cfg(name: str, default: Any) -> Any:
    value = getattr(get_settings(), name, None)
    return default if value in (None, "") else value


def accept_language(locale: str = "") -> str:
    loc = (locale or str(_cfg("browser_locale", "en-IN"))).strip() or "en-IN"
    base = loc.split("-")[0]
    if base == loc:
        return f"{loc},en;q=0.9" if loc != "en" else "en"
    return f"{loc},{base};q=0.9" if base == "en" else f"{loc},{base};q=0.9,en;q=0.8"


def basic_headers(mobile: bool = False) -> dict[str, str]:
    """A short header set (UA + Accept). Some bot managers score a full Chrome header set sent over a non-Chrome
    TLS stack as forged, so this is tried first; ``chrome_headers`` is the second try."""
    return {
        "User-Agent": _UA_MOBILE if mobile else _UA_DESKTOP,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }


HEADER_PROFILES = ("basic", "chrome")


def chrome_headers(mobile: bool = False, locale: str = "") -> dict[str, str]:
    """The header set a real Chrome sends on a top-level navigation (UA and client hints agree)."""
    platform = '"Android"' if mobile else '"Windows"'
    return {
        "User-Agent": _UA_MOBILE if mobile else _UA_DESKTOP,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": accept_language(locale),
        "Accept-Encoding": "gzip, deflate, br" if _has_brotli() else "gzip, deflate",
        "Sec-Ch-Ua": f'"Google Chrome";v="{CHROME_MAJOR}", "Chromium";v="{CHROME_MAJOR}", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?1" if mobile else "?0",
        "Sec-Ch-Ua-Platform": platform,
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


def _has_brotli() -> bool:
    try:
        import brotli  # noqa: F401

        return True
    except Exception:
        try:
            import brotlicffi  # noqa: F401

            return True
        except Exception:
            return False


# Proxy -------------------------------------------------------------------------------------------------------
def proxy_url() -> str:
    return str(_cfg("browser_proxy_url", "") or "").strip()


def parse_proxy(raw: str) -> dict | None:
    """'http://user:pass@host:port' -> Playwright proxy dict {server, username, password}. None when unset/bad."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    p = urlparse(raw)
    if p.scheme not in {"http", "https", "socks5", "socks5h"} or not p.hostname:
        return None
    server = f"{p.scheme}://{p.hostname}" + (f":{p.port}" if p.port else "")
    out: dict[str, str] = {"server": server}
    if p.username:
        from urllib.parse import unquote

        out["username"] = unquote(p.username)
        out["password"] = unquote(p.password or "")
    return out


def redact(text: str) -> str:
    """Remove a configured proxy URL or credentials from error text before it is logged or shown."""
    out = str(text or "")
    raw = proxy_url()
    if raw:
        out = out.replace(raw, "<proxy>")
        info = parse_proxy(raw) or {}
        if info.get("password"):
            out = out.replace(info["password"], "***")
    return re.sub(r"(apiKey|api_key|signingKey|token)=[^&\s\"']+", r"\1=***", out)


_clients: dict[str, httpx.AsyncClient] = {}


def _client(proxied: bool) -> httpx.AsyncClient | None:
    key = "proxy" if proxied else "direct"
    client = _clients.get(key)
    if client is not None and not client.is_closed:
        return client
    kw: dict[str, Any] = {
        "timeout": httpx.Timeout(20.0, connect=6.0),
        "limits": httpx.Limits(max_connections=60, max_keepalive_connections=20),
        "http2": True,
        "follow_redirects": True,
    }
    if proxied:
        raw = proxy_url()
        if not raw:
            return None
        if raw.startswith("socks"):
            try:
                import socksio  # noqa: F401
            except Exception:
                log.warning("browsing: BROWSER_PROXY_URL is SOCKS but socksio is not installed; proxied fetch skipped")
                return None
        kw["proxy"] = raw if "://" in raw else "http://" + raw
    try:
        client = httpx.AsyncClient(**kw)
    except Exception as exc:  # noqa: BLE001 - e.g. h2 missing
        log.warning("browsing: http client setup failed: %s", redact(str(exc))[:200])
        kw["http2"] = False
        client = httpx.AsyncClient(**kw)
    _clients[key] = client
    return client


# Page helpers ------------------------------------------------------------------------------------------------
def _page(url: str, title: str, text: str, html: str, status: int | None, via: str) -> dict:
    from app.connectors.web_fetch import wrap_html

    return {
        "ok": True,
        "url": url,
        "title": (title or url)[:200],
        "text": text[:12000],
        "html": wrap_html(html[:400000], url) if html else text_to_html(text, url, title),
        "status": status,
        "fetched_via": via,
    }


def text_to_html(text: str, url: str, title: str = "") -> str:
    import html as html_lib

    body = "".join(f"<p>{html_lib.escape(p)}</p>" for p in re.split(r"\n{2,}", text or "") if p.strip())
    links = "".join(
        f'<a href="{html_lib.escape(href)}">{html_lib.escape(label)}</a> '
        for label, href in re.findall(r"\[([^\]]{1,120})\]\((https?://[^)\s]+)\)", text or "")[:200]
    )
    return (
        f"<!doctype html><html><head><base href=\"{html_lib.escape(url)}\"><meta charset='utf-8'>"
        f"<title>{html_lib.escape(title or url)}</title></head><body>{body}<nav>{links}</nav></body></html>"
    )


def judge(page: dict | None, *, shell_is_block: bool = True) -> dict | None:
    """The block (or thin-shell) verdict for a fetched page; None when it is usable."""
    page = page or {}
    if page.get("block"):
        return page["block"]
    return detect_block(
        page.get("status"),
        str(page.get("url") or ""),
        str(page.get("title") or ""),
        str(page.get("text") or ""),
        str(page.get("raw_html") or page.get("html") or ""),
        shell_is_block=shell_is_block,
    )


async def http_get(
    url: str, *, proxied: bool = False, mobile: bool = False, timeout: float = 15.0, profiles: tuple[str, ...] = HEADER_PROFILES
) -> dict | None:
    """GET with browser headers: the short set first, then (only if that was blocked) the full Chrome set,
    sharing ``timeout``. Returns a page dict with ``block`` set when the response is a wall."""
    deadline = time.monotonic() + timeout
    page: dict | None = None
    for i, profile in enumerate(profiles):
        left = deadline - time.monotonic()
        if i and left < 2:
            break
        # A network error propagates: it is not a header problem, so there is no point retrying here.
        page = await _http_get_once(url, proxied=proxied, mobile=mobile, timeout=max(1.0, left), profile=profile)
        if page is None or not page.get("block"):
            return page
    return page


async def _http_get_once(url: str, *, proxied: bool, mobile: bool, timeout: float, profile: str) -> dict | None:
    from app.connectors.web_fetch import html_to_text

    client = _client(proxied)
    if client is None:
        return None
    headers = chrome_headers(mobile=mobile) if profile == "chrome" else basic_headers(mobile=mobile)
    resp = await client.get(url, headers=headers, timeout=httpx.Timeout(timeout, connect=min(6.0, timeout)))
    raw = resp.text or ""
    title, text = html_to_text(raw)
    final = str(resp.url) if resp.url else url
    page = _page(final, title, text, raw, resp.status_code, "proxy" if proxied else "server")
    page["raw_html"] = raw[:200000]
    page["headers_profile"] = profile
    block = detect_block(resp.status_code, final, title, text, raw)
    if block:
        page.update(ok=False, block=block)
    elif resp.status_code >= 400:
        page["ok"] = False
    return page


# Tavily ------------------------------------------------------------------------------------------------------
_CACHE: "OrderedDict[str, tuple[float, str, str]]" = OrderedDict()
_CACHE_TTL = 900.0
_CACHE_MAX = 300


def _norm(url: str) -> str:
    try:
        p = urlparse((url or "").strip())
    except Exception:
        return url or ""
    host = (p.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    path = p.path.rstrip("/") or "/"
    return urlunparse(("", host, path, "", p.query, ""))


def cache_put(url: str, title: str, text: str) -> None:
    text = (text or "").strip()
    if not url or len(text) < THIN_TEXT:
        return
    if detect_block(None, url, title, text):
        return
    key = _norm(url)
    _CACHE[key] = (time.monotonic(), title or "", text[:20000])
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)


def cache_get(url: str) -> dict | None:
    key = _norm(url)
    row = _CACHE.get(key)
    if not row:
        return None
    ts, title, text = row
    if time.monotonic() - ts > _CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    page = _page(url, title, text, "", 200, "tavily_cache")
    page["note"] = "Text Tavily returned with the search results (no interactive page)."
    return page


def tavily_key() -> str:
    return str(_cfg("tavily_api_key", "") or "").strip()


async def tavily_extract(url: str, timeout: float = 20.0, depth: str = "basic") -> dict | None:
    key = tavily_key()
    if not key or not _cfg("tavily_extract", True):
        return None
    client = _client(False)
    resp = await client.post(
        "https://api.tavily.com/extract",
        json={"urls": [url], "extract_depth": depth, "format": "markdown", "timeout": max(1, min(int(timeout), 60))},
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout=httpx.Timeout(timeout + 5, connect=6.0),
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Tavily Extract HTTP {resp.status_code}")
    data = resp.json() or {}
    rows = [r for r in data.get("results") or [] if isinstance(r, dict) and r.get("raw_content")]
    if not rows:
        failed = data.get("failed_results") or []
        why = str((failed[0] or {}).get("error") if failed and isinstance(failed[0], dict) else "no content")[:120]
        raise RuntimeError(f"Tavily Extract: {why}")
    text = str(rows[0]["raw_content"])
    title = _md_title(text) or url
    page = _page(str(rows[0].get("url") or url), title, text, "", 200, "tavily_extract")
    block = detect_block(None, url, title, text)
    if block:
        page.update(ok=False, block=block)
    return page


def _md_title(text: str) -> str:
    m = re.search(r"^\s*#\s+(.{3,160})$", text or "", re.M)
    return m.group(1).strip() if m else ""


# Hosted browsers ---------------------------------------------------------------------------------------------
def remote_provider() -> str:
    name = str(_cfg("remote_browser_provider", "") or "").strip().lower()
    if name == "steel" and _cfg("steel_api_key", ""):
        return "steel"
    if name == "browserbase" and _cfg("browserbase_api_key", "") and _cfg("browserbase_project_id", ""):
        return "browserbase"
    return ""


async def steel_scrape(url: str, timeout: float = 60.0) -> dict | None:
    """Steel's scrape endpoint renders the page in Steel's hosted browser (key stays on the API server)."""
    from app.connectors.web_fetch import html_to_text

    key = str(_cfg("steel_api_key", "") or "")
    if not key:
        return None
    body: dict[str, Any] = {"url": url, "format": ["html", "markdown"], "delay": 2}
    if _cfg("remote_browser_use_proxy", False):
        body["useProxy"] = True
    resp = await _client(False).post(
        "https://api.steel.dev/v1/scrape",
        json=body,
        headers={"steel-api-key": key, "Content-Type": "application/json"},
        timeout=httpx.Timeout(timeout, connect=6.0),
    )
    if resp.status_code >= 400:
        raise RuntimeError({402: "Steel: plan has no access (402)", 429: "Steel: limit reached (429)", 503: "Steel: no capacity (503)"}.get(resp.status_code, f"Steel HTTP {resp.status_code}"))
    data = resp.json() or {}
    content = data.get("content") or {}
    html = str(content.get("html") or "")
    md = str(content.get("markdown") or "")
    meta = data.get("metadata") or {}
    title, text = html_to_text(html) if html else ("", "")
    text = md or text
    final = str(meta.get("urlSource") or meta.get("url") or url)
    status = meta.get("statusCode")
    page = _page(final, str(meta.get("title") or title or final), text, html, int(status) if status else 200, "remote:steel")
    block = detect_block(page["status"], final, page["title"], text, html)
    if block:
        page.update(ok=False, block=block)
    return page


async def browserbase_session() -> str:
    """A fresh Browserbase session's CDP connect URL (connect within 5 minutes). The URL is session-scoped."""
    body: dict[str, Any] = {"projectId": str(_cfg("browserbase_project_id", ""))}
    if _cfg("remote_browser_use_proxy", False):
        body["proxies"] = True
    resp = await _client(False).post(
        "https://api.browserbase.com/v1/sessions",
        json=body,
        headers={"X-BB-API-Key": str(_cfg("browserbase_api_key", "")), "Content-Type": "application/json"},
        timeout=httpx.Timeout(20.0, connect=6.0),
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Browserbase HTTP {resp.status_code}")
    url = str((resp.json() or {}).get("connectUrl") or "")
    if not url:
        raise RuntimeError("Browserbase returned no connectUrl")
    return url


# Alternates --------------------------------------------------------------------------------------------------
def alternate_urls(url: str, html: str = "") -> list[tuple[str, str, bool]]:
    """(label, url, mobile UA?) other public versions of the same page: AMP / mobile links the page declares,
    then an m. subdomain guess. Never different content."""
    out: list[tuple[str, str, bool]] = []
    raw = html or ""
    for rel, label, mobile in (("amphtml", "amp", True), ("alternate", "mobile", True)):
        for tag in re.findall(r"<link\b[^>]*>", raw[:200000], re.I):
            low = tag.lower()
            if f'rel="{rel}"' not in low and f"rel='{rel}'" not in low:
                continue
            if rel == "alternate" and "handheld" not in low and "only screen and (max-width" not in low:
                continue
            m = re.search(r"href=[\"']([^\"']+)", tag, re.I)
            if m and m.group(1).startswith("http"):
                out.append((label, m.group(1), mobile))
                break
    p = urlparse(url)
    host = p.hostname or ""
    if host and not host.startswith(("m.", "amp.")) and host.count(".") >= 1:
        bare = host[4:] if host.startswith("www.") else host
        out.append(("mobile", urlunparse((p.scheme, "m." + bare, p.path, "", p.query, "")), True))
    seen: set[str] = set()
    uniq = []
    for row in out:
        if row[1] not in seen and row[1] != url:
            seen.add(row[1])
            uniq.append(row)
    return uniq[:2]


async def reader_fetch(url: str, timeout: float = 20.0) -> dict | None:
    base = str(_cfg("browse_reader_url", "") or "").strip()
    if not base:
        return None
    headers = {"Accept": "text/plain", "X-Return-Format": "markdown"}
    if _cfg("jina_api_key", ""):
        headers["Authorization"] = f"Bearer {_cfg('jina_api_key', '')}"
    resp = await _client(False).get(base.rstrip("/") + "/" + url, headers=headers, timeout=httpx.Timeout(timeout, connect=6.0))
    if resp.status_code >= 400:
        raise RuntimeError(f"reader HTTP {resp.status_code}")
    text = resp.text or ""
    m = re.search(r"^Title:\s*(.+)$", text, re.M)
    title = m.group(1).strip() if m else ""
    body = text.split("Markdown Content:", 1)[-1].strip() if "Markdown Content:" in text else text
    code = re.search(r"^Warning: Target URL returned error (\d{3})", text, re.M)
    status = int(code.group(1)) if code else 200
    page = _page(url, title or url, body, "", status, "reader")
    block = detect_block(status, url, title, body)
    if block:
        page.update(ok=False, block=block)
    return page


async def archive_fetch(url: str, timeout: float = 20.0) -> dict | None:
    if not _cfg("browse_archive", True):
        return None
    client = _client(False)
    resp = await client.get("https://archive.org/wayback/available", params={"url": url}, timeout=httpx.Timeout(min(timeout, 10.0), connect=5.0))
    snap = (((resp.json() or {}).get("archived_snapshots") or {}).get("closest") or {}) if resp.status_code < 400 else {}
    if not snap.get("available") or not snap.get("url"):
        raise RuntimeError("no archived copy")
    page = await http_get(str(snap["url"]).replace("http://", "https://", 1), timeout=timeout)
    if page is None:
        return None
    stamp = str(snap.get("timestamp") or "")
    when = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}" if len(stamp) >= 8 else "an earlier date"
    page["fetched_via"] = "archive"
    page["archived"] = when
    page["note"] = f"Archived copy from {when} (Internet Archive); prices and stock may be out of date. Say so."
    return page


# The ladder --------------------------------------------------------------------------------------------------
StepFn = Callable[[str, float, dict], Awaitable[dict | None]]


def _usable(page: dict | None) -> tuple[bool, dict | None]:
    if not isinstance(page, dict):
        return False, None
    block = judge(page)
    if block:
        return False, block
    if page.get("ok") is False or page_is_thin(str(page.get("text") or "")):
        return False, None
    return True, None


def blocked_report(url: str, attempts: list[dict], first_block: dict | None = None) -> dict:
    host = host_of(url) or url
    tried = ", ".join(
        a["step"] + (f" ({a.get('kind') or a.get('error')})" if a.get("kind") or a.get("error") else "") for a in attempts
    ) or "nothing"
    kinds = {a.get("kind") for a in attempts if a.get("kind")}
    reason = (first_block or {}).get("reason") or (
        f"{host} could not be read" + (" (bot protection)" if kinds else "") + "."
    )
    return {
        "ok": False,
        "blocked": True,
        "blocked_source": host,
        "url": url,
        "title": "",
        "text": "",
        "error": f"{reason} Tried: {tried}.",
        "attempts": attempts,
        "hint": (
            f"Tell the user plainly that {host} blocked automated access (do not say the browser is restricted), "
            "then continue with other sites that have the same information (official sites, comparison or "
            "review sites, news). Never invent what the page says. If the user needs this exact site, they can "
            "open it with take-control on the live computer."
        ),
    }


async def fetch_with_ladder(
    url: str,
    steps: tuple[str, ...] | list[str] = LADDER_READ,
    *,
    browser: StepFn | None = None,
    total_s: float | None = None,
    step_s: float | None = None,
    hints: dict | None = None,
    custom: dict[str, StepFn] | None = None,
) -> dict:
    """Walk ``steps`` in order until one returns a usable page. Each step is capped at ``step_s`` and the whole
    ladder at ``total_s``. ``browser`` is an optional step named "browser" (the computer's real browser);
    ``custom`` can add or override steps (tests, the hosted browser over CDP). Never raises."""
    total = float(total_s if total_s is not None else _cfg("browse_total_timeout_s", 60.0))
    per = float(step_s if step_s is not None else _cfg("browse_step_timeout_s", 20.0))
    remote_per = max(per, float(_cfg("browse_remote_timeout_s", 60.0)))
    state: dict = {"html": "", **(hints or {})}
    fns: dict[str, StepFn] = {**_STEPS, **({"browser": browser} if browser else {}), **(custom or {})}
    attempts: list[dict] = []
    first_block: dict | None = None
    started = time.monotonic()
    for step in steps:
        fn = fns.get(step)
        if fn is None:
            continue
        left = total - (time.monotonic() - started)
        if left <= 0.5:
            attempts.append({"step": step, "ok": False, "error": f"skipped (ladder time limit {total:g}s used up)"})
            break
        limit = min(remote_per if step in {"remote", "browser"} else per, left)
        t0 = time.monotonic()
        row: dict[str, Any] = {"step": step}
        try:
            page = await asyncio.wait_for(fn(url, limit, state), timeout=limit)
        except (asyncio.TimeoutError, TimeoutError):
            row.update(ok=False, error=f"timed out after {limit:.0f}s")
            page = None
        except Exception as exc:  # noqa: BLE001 - one broken step must not stop the ladder
            row.update(ok=False, error=redact(f"{type(exc).__name__}: {exc}")[:160])
            page = None
        row["ms"] = int((time.monotonic() - t0) * 1000)
        if page is None and "error" not in row:
            row.update(ok=False, error="not configured")
            row["skipped"] = True
        if isinstance(page, dict):
            if page.get("raw_html") and not state.get("html"):
                state["html"] = str(page["raw_html"])
            ok, block = _usable(page)
            row.update(ok=ok, status=page.get("status"))
            if block:
                row.update(blocked=True, kind=block.get("kind"), vendor=block.get("vendor"))
                first_block = first_block or block
            elif not ok:
                row["error"] = "empty or unfinished page"
            if ok:
                attempts.append(row)
                page.pop("raw_html", None)
                page.pop("block", None)
                page["ok"] = True
                page["attempts"] = [a for a in attempts if not a.get("skipped")]
                if step not in {"server", "browser"}:
                    page.setdefault("note", f"Read via {page.get('fetched_via') or step} because the direct request was blocked or empty.")
                return page
        attempts.append(row)
    shown = [a for a in attempts if not a.get("skipped")]
    log.info("browsing: %s blocked/unreadable after %s", host_of(url), ", ".join(f"{a['step']}={a.get('kind') or a.get('error', '')}" for a in shown)[:400])
    return blocked_report(url, shown, first_block)


async def _step_cache(url: str, limit: float, state: dict) -> dict | None:
    return cache_get(url)


async def _step_server(url: str, limit: float, state: dict) -> dict | None:
    from app.connectors import web_fetch

    page = await web_fetch.fetch_public_page(url)
    if isinstance(page, dict):
        page.setdefault("fetched_via", "server")
    return page


async def _step_proxy(url: str, limit: float, state: dict) -> dict | None:
    if not proxy_url() or not _cfg("browser_proxy_for_fetch", True):
        return None
    return await http_get(url, proxied=True, timeout=limit)


async def _step_tavily(url: str, limit: float, state: dict) -> dict | None:
    return await tavily_extract(url, timeout=max(5.0, limit - 3))


async def _step_remote(url: str, limit: float, state: dict) -> dict | None:
    if remote_provider() == "steel":
        return await steel_scrape(url, timeout=limit)
    cdp = state.get("remote_cdp")
    if remote_provider() == "browserbase" and cdp is not None:
        return await cdp(url, limit)
    return None


async def _step_alternates(url: str, limit: float, state: dict) -> dict | None:
    if not _cfg("browse_alternates", True):
        return None
    alts = alternate_urls(url, str(state.get("html") or ""))
    last: dict | None = None
    deadline = time.monotonic() + limit
    for label, alt, mobile in alts:
        left = deadline - time.monotonic()
        if left < 2:
            break
        try:
            page = await http_get(alt, mobile=mobile, timeout=min(left, 12.0))
        except Exception:  # noqa: BLE001 - e.g. no m. host
            continue
        if page is None:
            continue
        page["fetched_via"] = f"alternate:{label}"
        page["note"] = f"Read the site's {label} version ({alt}); same site, lighter page."
        ok, _ = _usable(page)
        if ok:
            return page
        last = page
    return last


async def _step_reader(url: str, limit: float, state: dict) -> dict | None:
    return await reader_fetch(url, timeout=limit)


async def _step_archive(url: str, limit: float, state: dict) -> dict | None:
    return await archive_fetch(url, timeout=limit)


_STEPS: dict[str, StepFn] = {
    "cache": _step_cache,
    "server": _step_server,
    "proxy": _step_proxy,
    "tavily": _step_tavily,
    "remote": _step_remote,
    "alternates": _step_alternates,
    "reader": _step_reader,
    "archive": _step_archive,
}


# Browser daemon configuration ----------------------------------------------------------------------------------
async def user_timezone(user_id: str) -> str:
    import uuid

    try:
        uid = uuid.UUID(str(user_id))
    except Exception:
        return ""
    try:
        from app.db import SessionLocal
        from app.models import User

        async with SessionLocal() as db:
            user = await db.get(User, uid)
            return str(getattr(user, "timezone", "") or "")
    except Exception:  # noqa: BLE001 - no DB in tests / at boot
        return ""


def daemon_config(timezone: str = "") -> dict:
    """What the computer's browser daemon launches with (written to the sandbox, then deleted by the daemon).
    ``hash`` lets the API restart a daemon whose settings changed."""
    from app.connectors.computer_daemon import config_hash

    tz = str(_cfg("browser_timezone", "") or timezone or "Asia/Kolkata")
    if tz.upper() == "UTC":
        tz = "Asia/Kolkata"
    cfg: dict[str, Any] = {
        "locale": str(_cfg("browser_locale", "en-IN")),
        "timezone": tz,
        "http2": bool(_cfg("browser_http2", True)),
        "challenge_wait_s": float(_cfg("browser_challenge_wait_s", 15.0)),
        # Fast browser (docs/BROWSING.md): event-based waits instead of fixed sleeps; direct file fetches.
        "smart_waits": bool(_cfg("browser_smart_waits", True)),
        "direct_downloads": bool(_cfg("browser_direct_downloads", True)),
    }
    channel = str(_cfg("browser_channel", "") or "").strip().lower()
    if channel in {"chrome", "chrome-beta", "msedge"}:
        cfg["channel"] = channel
    proxy = parse_proxy(proxy_url())
    if proxy:
        cfg["proxy"] = proxy
    cfg["hash"] = config_hash(cfg)
    return cfg


def daemon_config_bytes(cfg: dict) -> bytes:
    return json.dumps(cfg).encode("utf-8")

