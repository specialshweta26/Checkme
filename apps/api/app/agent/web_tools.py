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


async def web_search(query: str, count: int = 8) -> dict:
    q = (query or "").strip()
    if not q:
        return {"error": "query is required"}
    limit = max(1, min(int(count or 8), 12))
    try:
        resp = await shared_client().post(
            "https://html.duckduckgo.com/html/",
            data={"q": q},
            headers={"User-Agent": _UA, "Accept": "text/html"},
            follow_redirects=True,
            timeout=15.0,
        )
        hits = parse_ddg_html(resp.text or "", limit)
    except Exception as exc:  # noqa: BLE001
        log.warning("web_search html failed: %s", exc)
        hits = []
    if not hits:
        try:
            instant = await shared_client().get(
                "https://api.duckduckgo.com/",
                params={"q": q, "format": "json", "no_html": 1, "no_redirect": 1},
                headers={"User-Agent": _UA, "Accept": "application/json"},
                timeout=10.0,
            )
            data = instant.json() if instant.status_code < 400 else {}
            abstract = (data.get("AbstractText") or data.get("Abstract") or "").strip()
            source = data.get("AbstractURL") or data.get("Redirect") or ""
            related = data.get("RelatedTopics") or []
            if abstract:
                hits.append({"title": data.get("Heading") or q, "url": source, "snippet": abstract[:400]})
            for item in related:
                if not isinstance(item, dict):
                    continue
                text = (item.get("Text") or "").strip()
                url = (item.get("FirstURL") or "").strip()
                if text and url:
                    hits.append({"title": text.split(" - ")[0][:200], "url": url, "snippet": text[:400]})
                if len(hits) >= limit:
                    break
        except Exception as exc:  # noqa: BLE001
            log.warning("web_search instant failed: %s", exc)
    return {"query": q, "results": hits[:limit], "source": "duckduckgo"}


async def web_fetch(ctx, url: str) -> Any:
    target = (url or "").strip()
    if not target:
        return {"error": "url is required"}
    if computer_slug(ctx):
        painted = await run_computer(ctx, "web_fetch", {"url": target})
        if not (isinstance(painted, dict) and painted.get("error")):
            return painted
    from app.connectors.web_fetch import fetch_public_page

    return await fetch_public_page(target)


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
