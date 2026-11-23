"""Tools an agent's own instructions rule out, taken away instead of only asked for in the prompt.

Live: Baba's instructions said "always use only skill to answer dont use browser", yet asked about One8's return policy
it ran web_search, because the prompt treats skills as guidance and a model may decide the web is better. Words in the
prompt are a request; a missing tool is a rule.

- "don't use (the) browser" → no browser tools.
- "don't use the web / internet / Google / web search" → no web search or page fetching.
- "only use (the / my) skill(s)" → no browser and no web search. Fetching a page stays when the request itself names a
  link (a skill's ingest step reads the page the user gave); without a link, page fetching is off too.

Only the agent's own instructions count, never text from a tool result or a page.
"""

from __future__ import annotations

import re

_NOT = r"(?:don'?t|dont|do not|never|no)\s+(?:use\s+|open\s+)?(?:the\s+|a\s+|any\s+)?"
NO_BROWSER = re.compile(_NOT + r"(?:web\s+)?browsers?\b", re.I)
NO_WEB = re.compile(_NOT + r"(?:web\s*search(?:es)?|internet|google|online\s+search|web)\b(?!\s*browser)", re.I)
SKILL_ONLY = re.compile(
    r"\bonly\s+(?:use\s+)?(?:the\s+|my\s+|your\s+|its\s+)?skills?\b|\buse\s+only\s+(?:the\s+|my\s+|your\s+|its\s+)?skills?\b",
    re.I,
)
LINK = re.compile(r"https?://\S+", re.I)

BROWSER_PREFIXES = ("browser_", "browse_")
SEARCH_TOOLS = frozenset({"web_search", "research_crawl", "browser_search"})
FETCH_TOOLS = frozenset({"web_read", "web_fetch"})


def limits(instructions: str, request: str = "") -> dict:
    """``{"browser": bool, "search": bool, "fetch": bool, "why": str}``: True means that kind of tool is off."""
    text = instructions or ""
    skill_only = bool(SKILL_ONLY.search(text))
    no_web = bool(NO_WEB.search(text))
    browser = skill_only or no_web or bool(NO_BROWSER.search(text))
    search = skill_only or no_web
    fetch = (skill_only or no_web) and not LINK.search(request or "")
    why = []
    if skill_only:
        why.append("answer with your skills only")
    if no_web:
        why.append("no web")
    if browser and not why:
        why.append("no browser")
    return {"browser": browser, "search": search, "fetch": fetch, "why": ", ".join(why)}


def blocked(name: str, lim: dict) -> bool:
    if lim.get("browser") and name.startswith(BROWSER_PREFIXES):
        return True
    if lim.get("search") and name in SEARCH_TOOLS:
        return True
    return bool(lim.get("fetch") and name in FETCH_TOOLS)


def apply(tools: list, instructions: str, request: str = "") -> tuple[list, str]:
    """The tools left, and one prompt line saying which are off and why ("" when nothing is)."""
    lim = limits(instructions, request)
    if not (lim["browser"] or lim["search"] or lim["fetch"]):
        return tools, ""
    kept = [t for t in tools if not blocked(str(getattr(t, "name", "")), lim)]
    off = [n for n, on in (("the browser", lim["browser"]), ("web search", lim["search"]), ("fetching web pages", lim["fetch"])) if on]
    note = (
        f"\n\nYour standing instructions ({lim['why']}) turn off {', '.join(off)} for you; those tools are not available. "
        "Answer from your skills and the user's files. If they don't have the answer, say what you checked and that it isn't there."
    )
    return kept, note
