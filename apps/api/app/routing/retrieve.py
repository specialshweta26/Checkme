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

GATEWAY_SEARCH_NAMES = ("gateway__tool_search", "find_tools", "tool_search")
GATEWAY_EXECUTE_NAMES = ("gateway__execute", "execute")
GATEWAY_RESOLVE_NAMES = ("gateway__resolve", "resolve")
EXECUTE_TOOLS = frozenset(GATEWAY_EXECUTE_NAMES)

_CAP_EXPAND = {
    "inventory": ("stock",),
    "stock": ("inventory",),
}


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


def is_execute_tool(name: str) -> bool:
    return (name or "").lower() in EXECUTE_TOOLS


def gateway_search_query(query: str) -> str:
    """Capability phrase for a nested gateway catalog (omit entity codes, expand synonyms)."""
    terms = [t for t in capability_terms(query) if not re.search(r"\d", t)]
    extra: list[str] = []
    for term in terms:
        for alias in _CAP_EXPAND.get(term, ()):
            if alias not in terms and alias not in extra:
                extra.append(alias)
    text = " ".join([*terms, *extra])
    return text or capability_query(query)


def search_call_args(schema: dict | None, query: str) -> dict:
    props = (schema or {}).get("properties") or {}
    for key in ("query", "q", "search", "text"):
        if key in props:
            return {key: query}
    return {"query": query}


def resolve_call_args(schema: dict | None, tool_name: str) -> dict:
    props = (schema or {}).get("properties") or {}
    for key in ("name", "tool_name", "public_name", "tool"):
        if key in props:
            return {key: tool_name}
    return {"name": tool_name}


def wrap_through_execute(tool_name: str, args: dict | None, execute_name: str | None) -> tuple[str, dict]:
    payload = dict(args or {})
    if not execute_name or is_execute_tool(tool_name) or is_catalog_tool(tool_name):
        return tool_name, payload
    if payload.get("tool_name") and isinstance(payload.get("arguments"), dict):
        return execute_name, payload
    return execute_name, {"tool_name": tool_name, "arguments": payload}


def nested_tool_rows(
    slug: str,
    connector_name: str,
    candidates: list[dict],
    execute_via: str,
    terms: list[str],
) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for item in candidates:
        name = str(item.get("public_name") or item.get("name") or "").strip()
        if not name or name in seen or is_catalog_tool(name) or is_execute_tool(name):
            continue
        seen.add(name)
        base = item.get("score")
        try:
            score = float(base) if base is not None else 0.55
        except (TypeError, ValueError):
            score = 0.55
        rows.append(
            {
                "connector_slug": slug,
                "connector_name": connector_name,
                "tool_name": name,
                "description": (item.get("description") or "")[:800],
                "group": str(item.get("group") or ""),
                "produces": [],
                "read_only": True,
                "destructive": False,
                "execute_via": execute_via,
                "score": retrieve_score(name, score, terms),
            }
        )
    rows.sort(key=lambda row: row["score"], reverse=True)
    return rows


def retrieve_score(tool_name: str, base: float, terms: list[str]) -> float:
    score = float(base)
    blob = (tool_name or "").lower().replace("_", " ")
    desc_hit = False
    if any(term in blob for term in terms):
        score += 0.2
        desc_hit = True
    if is_catalog_tool(tool_name) or is_execute_tool(tool_name):
        score *= 0.25
    elif desc_hit:
        score += 0.05
    return score
