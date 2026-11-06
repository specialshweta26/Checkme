"""Markdown (what the agent writes) -> WhatsApp formatting, and splitting long replies into messages.

WhatsApp: *bold*, _italic_, ~strike~, ```code```, `inline`. Max 4096 characters per text message."""

from __future__ import annotations

import re

LIMIT = 4096
_FENCE = re.compile(r"```[^\n`]*\n?(.*?)```", re.S)
_INLINE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", re.S)
_ITALIC_STAR = re.compile(r"(?<![\*\w])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\*\w])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.S)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.M)
_LINK = re.compile(r"!?\[([^\]\n]*)\]\((https?://[^)\s]+)\)")
_BULLET = re.compile(r"^(\s*)[-*+]\s+", re.M)
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$", re.M)

B = "\x01"  # placeholder for a bold marker while italics are converted


def to_whatsapp(md: str) -> str:
    text = (md or "").replace("\r\n", "\n")
    keep: list[str] = []

    def stash(value: str) -> str:
        keep.append(value)
        return f"\x00{len(keep) - 1}\x00"

    text = _FENCE.sub(lambda m: stash("```" + m.group(1).strip("\n") + "```"), text)
    text = _INLINE.sub(lambda m: stash("`" + m.group(1) + "`"), text)

    def link(m: re.Match) -> str:
        label, url = m.group(1).strip(), m.group(2)
        return url if not label or label == url else f"{label} ({url})"

    text = _LINK.sub(lambda m: stash(link(m)), text)
    text = _RULE.sub("", text)
    text = _HEADING.sub(lambda m: f"{B}{m.group(1).strip().strip('*_')}{B}", text)
    text = _BOLD.sub(lambda m: f"{B}{m.group(2)}{B}", text)
    text = _BULLET.sub(lambda m: f"{m.group(1)}• ", text)
    text = _ITALIC_STAR.sub(lambda m: f"_{m.group(1)}_", text)
    text = _STRIKE.sub(lambda m: f"~{m.group(1)}~", text)
    text = text.replace(B, "*")
    text = re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _cut(text: str, size: int) -> int:
    """Where to split: the last paragraph break, else line break, else space, inside the window."""
    window = text[:size]
    for sep in ("\n\n", "\n", " "):
        at = window.rfind(sep)
        if at > size // 3:
            return at
    return size


def chunk(text: str, limit: int = LIMIT) -> list[str]:
    """Split into messages of at most ``limit`` characters; code fences are closed and reopened across a split.

    More than one part gets a "(i/n)" counter."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    room = limit - 12  # counter plus a re-opened/closed fence
    parts: list[str] = []
    rest = text
    carry = False  # the previous part ended inside a code block
    while rest:
        prefix = "```\n" if carry else ""
        budget = room - len(prefix)
        if len(rest) <= budget:
            piece, rest = rest, ""
        else:
            at = _cut(rest, budget)
            piece, rest = rest[:at].rstrip(), rest[at:].lstrip("\n ")
        piece = prefix + piece
        open_fence = piece.count("```") % 2 == 1
        if open_fence:
            piece += "```"
        carry = open_fence
        parts.append(piece)
    n = len(parts)
    return [f"{p}\n({i}/{n})" for i, p in enumerate(parts, 1)] if n > 1 else parts
