from __future__ import annotations

import html
import re

_BLOCK_TAGS = re.compile(r"(?i)<\s*(br|/p|/div|/li|/tr|/h[1-6])\b[^>]*>")
_DROP_BLOCKS = re.compile(r"(?is)<(script|style|head)\b.*?</\1>")
_TAGS = re.compile(r"(?s)<[^>]+>")
_SPACES = re.compile(r"[ \t\u00a0]+")
_BLANKS = re.compile(r"\n{3,}")

_QUOTE_MARKERS = [
    re.compile(r"(?im)^\s*On .{0,200}wrote:\s*$"),
    re.compile(r"(?im)^\s*-{2,}\s*Original Message\s*-{2,}"),
    re.compile(r"(?im)^\s*From:\s.+\n\s*(Sent|Date):\s.+"),
    re.compile(r"(?im)^\s*_{10,}\s*$"),
]
_SIGNATURE = re.compile(r"(?m)^--\s*$")


def html_to_text(raw: str) -> str:
    text = _DROP_BLOCKS.sub("", raw)
    text = _BLOCK_TAGS.sub("\n", text)
    text = _TAGS.sub("", text)
    text = html.unescape(text)
    text = _SPACES.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANKS.sub("\n\n", text).strip()


def strip_quoted(text: str) -> str:
    cut = len(text)
    for pattern in _QUOTE_MARKERS:
        m = pattern.search(text)
        if m and m.start() > 0:
            cut = min(cut, m.start())
    text = text[:cut]
    m = _SIGNATURE.search(text)
    if m and m.start() > 0:
        text = text[: m.start()]
    lines = [line for line in text.splitlines() if not line.lstrip().startswith(">")]
    return _BLANKS.sub("\n\n", "\n".join(lines)).strip()


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " ...[truncated]"


def clean_body(raw: str, is_html: bool = False, limit: int = 2000, keep_quoted: bool = False) -> str:
    text = html_to_text(raw) if is_html else raw.replace("\r\n", "\n")
    if not keep_quoted:
        text = strip_quoted(text)
    return clip(text, limit)
