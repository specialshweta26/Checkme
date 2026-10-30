"""Fetch a public page from the API network when the sandbox browser cannot."""

from __future__ import annotations

import re
from html.parser import HTMLParser



class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = False
        self._title: list[str] = []
        self._in_title = False
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip = True
        if tag == "title":
            self._in_title = True
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr"}:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip = False
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._skip:
            return
        text = re.sub(r"\s+", " ", data).strip()
        if not text:
            return
        if self._in_title:
            self._title.append(text)
        else:
            self.chunks.append(text + " ")

    def title(self) -> str:
        return " ".join(self._title).strip()[:200]

    def text(self) -> str:
        raw = re.sub(r"[ \t]+\n", "\n", "".join(self.chunks))
        return re.sub(r"\n{3,}", "\n\n", raw).strip()


def html_to_text(html: str) -> tuple[str, str]:
    parser = _Text()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:
        pass
    return parser.title(), parser.text()[:12000]


def wrap_html(html: str, url: str) -> str:
    body = html or "<p>No content</p>"
    if re.search(r"<base\b", body, re.I):
        return body
    base = f'<base href="{url}">'
    match = re.search(r"<head[^>]*>", body, re.I)
    if match:
        i = match.end()
        return body[:i] + base + body[i:]
    return f"<!doctype html><html><head>{base}<meta charset='utf-8'></head><body>{body}</body></html>"


def browser_failed(data: dict | None, requested: str = "") -> bool:
    data = data or {}
    url = str(data.get("url") or requested or "")
    err = str(data.get("error") or "").lower()
    text = str(data.get("text") or "").lower()
    if data.get("ok") is False:
        return True
    if "chrome-error" in url or "chromewebdata" in url:
        return True
    if any(token in err for token in ("err_connection", "err_name", "err_timed", "net::", "timeout")):
        return True
    if "this site can't be reached" in text or "this page isn't working" in text:
        return True
    return False


def needs_real_browser(page: dict | None) -> bool:
    """True when the server's static HTML would show an empty or blocked shell (JS apps, bot checks, errors)."""
    from app.connectors.computer_daemon import bot_wall, page_is_thin

    page = page or {}
    if page.get("ok") is False or page.get("block"):
        return True
    text = str(page.get("text") or "")
    if bot_wall(str(page.get("url") or ""), str(page.get("title") or ""), text):
        return True
    return page_is_thin(text)


async def fetch_public_page(url: str) -> dict:
    """One direct fetch with a full Chrome header set (HTTP/2, en-IN). ``block`` is set when the response is a
    bot wall / access denied page (shared detector), and then ``ok`` is False."""
    from app.connectors.browsing import http_get
    from app.connectors.daytona_box import safe_http_url

    target = safe_http_url(url)
    page = await http_get(target, timeout=15.0)
    if page is None:  # pragma: no cover - the direct client always exists
        raise RuntimeError("no HTTP client")
    page.pop("raw_html", None)
    if not page.get("text"):
        page["ok"] = False
    return page
