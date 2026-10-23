"""Generic catalog retrieval: capability queries, not hardcoded tool names."""

from __future__ import annotations

import re

_STOP = frozenset(
    {
        "a",
        "an",
        "the",
        "me",
        "my",
        "our",
        "your",
        "us",
        "we",
        "i",
        "please",
        "for",
        "of",
        "in",
        "on",
        "to",
        "from",
        "with",
        "about",
        "what",
        "which",
        "who",
        "how",
        "is",
        "are",
        "was",
        "were",
        "do",
        "does",
        "did",
        "get",
        "give",
        "show",
        "tell",
        "need",
        "want",
        "left",
        "much",
        "many",
        "any",
        "all",
        "this",
        "that",
        "today",
        "now",
        "current",
        "remaining",
        "vs",
        "versus",
        "and",
        "or",
        "into",
        "over",
        "under",
        "still",
        "just",
        "also",
    }
)

_CATALOG_NAMES = frozenset(
    {
        "gateway__tool_search",
        "gateway__resolve",
        "gateway__list_mcps",
        "find_tools",
        "load_group",
        "tool_search",
        "list_mcps",
        "resolve",
        "search_tools",
    }
)


def capability_terms(query: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", (query or "").lower())
    out: list[str] = []
    for word in words:
        if len(word) < 3 or word in _STOP or word in out:
            continue
        out.append(word)
    return out


def capability_query(query: str) -> str:
    return " ".join(capability_terms(query)) or (query or "").strip()


def embedding_query(query: str) -> str:
    """Embed a capability phrase. Entity codes with digits stay out of the vector."""
    terms = [t for t in capability_terms(query) if not re.search(r"\d", t)]
    text = " ".join(terms) or capability_query(query)
    return f"Find a connector tool for: {text}" if text else (query or "").strip()


def lexical_tsquery(query: str) -> str:
    terms = capability_terms(query)
    if not terms:
        return ""
    return " | ".join(f"{t}:*" for t in terms)


def name_patterns(query: str) -> list[str]:
    return [f"%{t}%" for t in capability_terms(query)]


def is_catalog_tool(name: str) -> bool:
    n = (name or "").lower()
    return n in _CATALOG_NAMES or n.endswith("__tool_search")


def retrieve_score(tool_name: str, base: float, terms: list[str]) -> float:
    score = float(base)
    blob = (tool_name or "").lower().replace("_", " ")
    if any(term in blob for term in terms):
        score += 0.2
    if is_catalog_tool(tool_name):
        score *= 0.25
    return score
