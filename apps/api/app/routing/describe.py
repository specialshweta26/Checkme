from __future__ import annotations

import re

_REF = re.compile(r"\b(?:from|using|via|on|with|in)\s+([a-z0-9][a-z0-9._-]{1,40})", re.I)


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def connector_labels(connector) -> set[str]:
    labels = {connector.slug.lower(), (connector.alias or "").lower(), connector.name.lower()}
    return {label for label in labels if len(label) >= 2}


def connector_blob(connector) -> str:
    return _norm(
        " ".join(
            [
                connector.name,
                connector.alias or "",
                connector.slug,
                getattr(connector, "description", "") or "",
                " ".join(connector.tags or []),
            ]
        )
    )


def mentioned_connectors(query: str, connectors: list) -> list:
    q = query.lower()
    refs = [m.group(1).lower() for m in _REF.finditer(query)]
    hits = []
    seen: set = set()
    for connector in connectors:
        labels = connector_labels(connector)
        blob_words = set(connector_blob(connector).split())
        named = any(label in q for label in labels if len(label) >= 3)
        referred = any(ref in labels or ref in blob_words for ref in refs if len(ref) >= 3)
        if not (named or referred):
            continue
        key = str(connector.id)
        if key in seen:
            continue
        seen.add(key)
        hits.append(connector)
    return hits


def description_score(query: str, connector) -> float:
    q = _norm(query)
    if not q:
        return 0.0
    words = [w for w in q.split() if len(w) >= 3]
    blob = connector_blob(connector)
    score = 0.0
    for label in connector_labels(connector):
        if len(label) >= 3 and label in query.lower():
            score += 0.5
    desc = _norm(getattr(connector, "description", "") or "")
    if desc and words:
        overlap = sum(1 for w in words if w in desc)
        score += min(0.55, overlap * 0.1)
        if overlap / len(words) >= 0.35:
            score += 0.2
    for ref in (m.group(1).lower() for m in _REF.finditer(query)):
        if len(ref) >= 3 and (ref in connector_labels(connector) or ref in blob.split()):
            score += 0.6
    return min(1.3, score)


def catalog_lines(connectors: list) -> str:
    lines = []
    for connector in connectors:
        label = connector.alias or connector.name
        desc = (getattr(connector, "description", "") or "").strip()
        kind = "mail+calendar" if connector.kind == "builtin" else "mcp"
        extra = f" — {desc}" if desc else ""
        if connector.kind == "builtin":
            extra += (
                " — mail and calendar: send_message, get_message, create_event, "
                "create_google_meet, create_teams_meeting"
            )
        lines.append(f"- @{connector.slug} ({label}, {kind}){extra}")
    return "\n".join(lines) if lines else "(none)"
