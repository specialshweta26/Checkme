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
        "list_apps",
        "connect_app",
        "list_connections",
        "list_toolkits",
        "composio_search_tools",
        "composio_manage_connections",
        "composio_get_tool_schemas",
        "composio_multi_execute_tool",
        "composio_execute_tool",
    }
)

GATEWAY_SEARCH_NAMES = (
    "gateway__tool_search",
    "find_tools",
    "tool_search",
    "COMPOSIO_SEARCH_TOOLS",
)
GATEWAY_EXECUTE_NAMES = ("gateway__execute", "execute", "COMPOSIO_MULTI_EXECUTE_TOOL", "COMPOSIO_EXECUTE_TOOL")
GATEWAY_RESOLVE_NAMES = ("gateway__resolve", "resolve", "COMPOSIO_GET_TOOL_SCHEMAS")
EXECUTE_TOOLS = frozenset(GATEWAY_EXECUTE_NAMES)

_CAP_EXPAND = {
    "inventory": ("stock",),
    "stock": ("inventory",),
}

_MAIL_QUERY_HINTS = (
    "email",
    "e-mail",
    "inbox",
    "gmail",
    "outlook",
    "mailbox",
    "spam",
    "draft",
)
_CALENDAR_QUERY_HINTS = (
    "invite",
    "meeting",
    "calendar",
    "event",
    "google meet",
    "free slot",
    "teams meeting",
)
_GENERIC_TOOL_TERMS = frozenset(
    {
        "search",
        "get",
        "list",
        "find",
        "show",
        "fetch",
        "query",
        "lookup",
        "message",
        "messages",
        "mail",
        "email",
        "inbox",
        "sender",
        "thread",
        "subject",
        "create",
        "send",
        "read",
        "open",
        "write",
        "update",
        "delete",
        "by",
    }
)


def _has_phrase(query: str, phrases: tuple[str, ...]) -> bool:
    q = (query or "").lower()
    for phrase in phrases:
        if " " in phrase or "-" in phrase:
            if phrase in q:
                return True
        elif re.search(rf"\b{re.escape(phrase)}\b", q):
            return True
    return False


def is_calendar_query(query: str) -> bool:
    return _has_phrase(query, _CALENDAR_QUERY_HINTS)


def is_mail_query(query: str) -> bool:
    q = query or ""
    if is_calendar_query(q) or _has_phrase(q, _MAIL_QUERY_HINTS):
        return True
    return bool(re.search(r"\bmails?\b", q.lower()))


def extra_capability_terms(query: str) -> list[str]:
    return [term for term in capability_terms(query) if term not in _GENERIC_TOOL_TERMS]


def matching_toolkit(connections: list, terms: list[str]) -> str | None:
    useful = [term for term in terms if len(term) >= 4]
    for item in connections:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("toolkit") or item.get("slug") or "").strip()
        if not slug:
            continue
        blob = slug.lower().replace("-", "_")
        for term in useful:
            if term in blob or blob in term:
                return slug
    return None


def keep_connector_scope(provider: str | None, query: str) -> bool:
    """Honor an explicit slug unless a mail connector was chosen for a non-mail capability."""
    if provider in {"gmail", "outlook"}:
        if is_mail_query(query) or is_calendar_query(query):
            return True
        return not extra_capability_terms(query)
    return True


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
    return (name or "").lower() in {item.lower() for item in EXECUTE_TOOLS}


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
