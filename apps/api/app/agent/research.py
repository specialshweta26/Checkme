"""Goal-directed crawl for long research tasks.

Fetches pages over plain HTTP in parallel (concurrency-limited, per-host politeness), extracts text and links,
scores links against the goal, and follows the most promising ones from a priority queue with page / depth / time
budgets and canonical-URL dedupe. Each page becomes a short note (extractive summary + its best passages), so the
model synthesizes from notes (map-reduce) instead of raw pages. State is checkpointed in Redis after every batch,
so a crawl survives timeouts and restarts and can be resumed with more budget. Pages that need a real browser
(thin JS shells, bot walls, failures) are returned as ``needs_browser`` for the computer tools.

Nothing here knows about any particular site or task.
"""

from __future__ import annotations

import asyncio
import heapq
import json
import logging
import math
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

log = logging.getLogger(__name__)

STATE_TTL_S = 7 * 24 * 3600
MAX_PAGES_CAP = 300
NOTE_CHARS = 900

_STOP = set(
    "a an and are as at be by for from has have how i in is it its of on or that the this to was were what when where "
    "which who why will with you your about into over than then there these those can do does did not no yes our we "
    "they them their my me best top find list show get more most also".split()
)
_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|msclkid$|mc_|ref$|ref_src$|igshid$|spm$)")
_ASSET = re.compile(r"\.(png|jpe?g|gif|webp|svg|ico|css|js|mjs|woff2?|ttf|zip|gz|mp4|mp3|avi|mov|dmg|exe|apk)(\?|$)", re.I)
# Generic low-value link areas (account, legal, sharing); scored down, never blocked.
_LOW_VALUE = re.compile(
    r"(login|log-in|signin|sign-in|signup|sign-up|register|account|cart|checkout|privacy|terms|cookie|legal|"
    r"careers|jobs|advertis|share|subscribe|newsletter|javascript:|mailto:|tel:)",
    re.I,
)


def terms(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9][a-z0-9\-]{1,}", (text or "").lower())
    return [w for w in words if w not in _STOP]


def canonical(url: str) -> str:
    """Dedupe key: lowercase scheme/host, no fragment, no tracking params, sorted query, no trailing slash."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url
    if parts.scheme not in {"http", "https"}:
        return ""
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = f":{parts.port}" if parts.port and parts.port not in {80, 443} else ""
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k.lower())))
    path = re.sub(r"/+$", "", parts.path or "") or "/"
    return urlunsplit(("https", host + port, path, query, ""))


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []
        self.base = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "base" and a.get("href") and not self.base:
            self.base = a["href"] or ""
        if tag == "a" and a.get("href"):
            self._href = a.get("href")
            self._text = [a.get("title") or a.get("aria-label") or ""]

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, re.sub(r"\s+", " ", " ".join(self._text)).strip()[:160]))
            self._href = None


def _merge_anchor(a: str, b: str) -> str:
    """Several links to one page: keep every distinct anchor (e.g. 'synchronize' and 'Synchronization Primitives')."""
    a, b = (a or "").strip(), (b or "").strip()
    if not b or b.lower() in a.lower():
        return a
    if not a or a.lower() in b.lower():
        return b
    return f"{a} / {b}"[:200]


def extract_links(html: str, base_url: str, limit: int = 400) -> list[tuple[str, str]]:
    parser = _LinkParser()
    try:
        parser.feed(html or "")
    except Exception:  # noqa: BLE001 - malformed HTML still yields what was parsed
        pass
    base = urljoin(base_url, parser.base) if parser.base else base_url
    out: list[tuple[str, str]] = []
    seen: dict[str, int] = {}
    for href, text in parser.links:
        href = (href or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        url = urljoin(base, href)
        if _ASSET.search(url) or urlsplit(url).scheme not in {"http", "https"}:
            continue
        key = canonical(url)
        if key and key in seen:
            out[seen[key]] = (out[seen[key]][0], _merge_anchor(out[seen[key]][1], text))
        elif key:
            seen[key] = len(out)
            out.append((url, text))
        if len(out) >= limit:
            break
    return out


def score_link(goal: list[str], url: str, text: str, *, parent_score: float = 0.0, same_host: bool = False) -> float:
    """0..~1: goal-term overlap in anchor text and URL path, plus a little inherited relevance."""
    if not goal:
        return 0.1
    g = set(goal)
    anchor = set(terms(text))
    path = set(terms(re.sub(r"[/_.?=&+-]", " ", urlsplit(url).path + " " + urlsplit(url).query)))
    hit_anchor = len(g & anchor) / len(g)
    hit_path = len(g & path) / len(g)
    if not (hit_anchor or hit_path):
        # No goal term at all: only a trace of the parent's relevance, below the default follow threshold.
        return round(0.05 * parent_score + (0.02 if same_host else 0.0), 4)
    score = 0.6 * hit_anchor + 0.3 * hit_path + 0.15 * parent_score + (0.03 if same_host else 0.0)
    if _LOW_VALUE.search(url) or _LOW_VALUE.search(text or ""):
        score *= 0.3
    return round(score, 4)


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    return [p.strip() for p in parts if 30 <= len(p.strip()) <= 600]


def page_score(goal: list[str], text: str) -> float:
    if not goal or not text:
        return 0.0
    words = terms(text[:60000])
    if not words:
        return 0.0
    counts: dict[str, int] = {}
    for w in words:
        counts[w] = counts.get(w, 0) + 1
    covered = sum(1 for t in set(goal) if counts.get(t))
    density = sum(math.log1p(counts.get(t, 0)) for t in set(goal)) / (len(set(goal)) * math.log1p(len(words)) + 1e-9)
    return round(0.7 * covered / len(set(goal)) + 0.3 * min(1.0, density * 4), 4)


def summarize(goal: list[str], title: str, text: str, max_chars: int = NOTE_CHARS) -> str:
    """Extractive note: the sentences that carry the most goal terms, in page order."""
    sents = _sentences(text)
    g = set(goal)
    ranked = sorted(range(len(sents)), key=lambda i: -len(g & set(terms(sents[i]))))
    keep: list[int] = []
    used = 0
    for i in ranked:
        if not g & set(terms(sents[i])) and keep:
            break
        if used + len(sents[i]) > max_chars:
            continue
        keep.append(i)
        used += len(sents[i]) + 1
    body = " ".join(sents[i] for i in sorted(keep)) or (text or "")[:max_chars]
    return f"{title.strip()[:160]}: {body}".strip(": ")


def needs_browser(page: dict) -> str:
    """Why a fetched page should be opened in the real browser instead ('' when the HTTP copy is fine)."""
    from app.connectors.computer_daemon import bot_wall, page_is_thin

    if not page.get("ok"):
        return f"fetch failed ({page.get('status') or page.get('error') or 'error'})"
    if bot_wall(page.get("url") or "", page.get("title") or "", (page.get("text") or "")[:4000]):
        return "bot wall"
    if page_is_thin(page.get("text") or ""):
        return "thin page (likely rendered by JavaScript)"
    return ""


@dataclass
class Note:
    url: str
    title: str
    depth: int
    score: float
    note: str
    links_followed: int = 0


@dataclass
class CrawlState:
    id: str
    goal: str
    max_pages: int = 30
    max_depth: int = 2
    same_site: bool = False
    frontier: list[list] = field(default_factory=list)  # heap of [-score, seq, url, depth, parent_score, anchor]
    seen: list[str] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)
    browser: list[dict] = field(default_factory=list)
    seq: int = 0
    pages: int = 0
    status: str = "running"
    stop_reason: str = ""
    seed_hosts: list[str] = field(default_factory=list)
    updated_at: float = 0.0
    reused: int = 0  # pages served from this user's research memory instead of a fetch
    scoring: str = "keywords"
    summary_saved: bool = False

    def push(self, url: str, depth: int, score: float, parent_score: float = 0.0, anchor: str = "") -> bool:
        key = canonical(url)
        if not key or key in self._seen_set():
            return False
        self.seen.append(key)
        self._seen_cache.add(key)
        self.seq += 1
        heapq.heappush(self.frontier, [-score, self.seq, url, depth, parent_score, anchor])
        return True

    def _seen_set(self) -> set[str]:
        if not hasattr(self, "_seen_cache"):
            self._seen_cache = set(self.seen)
        return self._seen_cache

    def to_json(self) -> str:
        data = asdict(self)
        return json.dumps(data)

    @classmethod
    def from_json(cls, raw: str | bytes) -> "CrawlState":
        data = json.loads(raw)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


Fetcher = Callable[[str], Awaitable[dict]]
Saver = Callable[[CrawlState], Awaitable[None]]
Reporter = Callable[[str], Awaitable[None]]


def _host(url: str) -> str:
    h = (urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


async def crawl(
    state: CrawlState,
    fetch: Fetcher,
    *,
    concurrency: int = 6,
    per_host: int = 2,
    time_budget_s: float = 240.0,
    min_link_score: float = 0.12,
    enough_relevant: int = 0,
    relevant_at: float = 0.6,
    save: Saver | None = None,
    report: Reporter | None = None,
    scorer: Any = None,
    store: Any = None,
    max_age_h: float = 72.0,
) -> CrawlState:
    """Best-first crawl until a budget or stop condition. Mutates and returns ``state``; ``save`` checkpoints it.

    ``scorer`` (relevance.Scorer) adds semantic link/page scores on top of keywords; ``store`` (research_store)
    serves fresh pages this user already read instead of fetching them, and remembers newly read pages.
    """
    from app.agent.relevance import Scorer

    scorer = scorer or Scorer(mode="keywords")
    goal = terms(state.goal)
    started = time.monotonic()
    sem = asyncio.Semaphore(max(1, concurrency))
    host_sems: dict[str, asyncio.Semaphore] = {}

    async def one(item: list) -> tuple[list, dict | None, Exception | None]:
        url = item[2]
        hs = host_sems.setdefault(_host(url), asyncio.Semaphore(max(1, per_host)))
        async with sem, hs:
            try:
                return item, await asyncio.wait_for(fetch(url), timeout=30), None
            except Exception as exc:  # noqa: BLE001 - one dead page never stops the crawl
                return item, None, exc

    state.status = "running"
    while True:
        if state.pages >= state.max_pages:
            state.stop_reason = "page budget reached"
            break
        if time.monotonic() - started > time_budget_s:
            state.stop_reason = "time budget for this call reached (resume to continue)"
            break
        if enough_relevant and sum(1 for n in state.notes if n["score"] >= relevant_at) >= enough_relevant:
            state.stop_reason = f"enough evidence ({enough_relevant} relevant pages)"
            break
        batch: list[list] = []
        while state.frontier and len(batch) < max(1, concurrency) and state.pages + len(batch) < state.max_pages:
            item = heapq.heappop(state.frontier)
            if -item[0] < min_link_score and item[3] > 0:
                continue  # never follow weak links; seeds (depth 0) always run
            batch.append(item)
        if not batch:
            state.stop_reason = "no promising links left"
            break
        remembered: dict[str, dict] = {}
        if store is not None:
            remembered = await store.fresh_pages([canonical(i[2]) for i in batch], max_age_h)
        to_fetch = [i for i in batch if canonical(i[2]) not in remembered]
        results = list(await asyncio.gather(*(one(item) for item in to_fetch)))
        results += [(i, remembered[canonical(i[2])], None) for i in batch if canonical(i[2]) in remembered]
        good: list[tuple[list, dict]] = []
        for item, page, exc in results:
            url = item[2]
            state.pages += 1
            if exc is not None or page is None:
                state.browser.append({"url": url, "reason": f"fetch error ({type(exc).__name__})"})
                continue
            why = needs_browser(page)
            if why:
                state.browser.append({"url": page.get("url") or url, "reason": why})
                continue
            good.append((item, page))
        texts = [(p.get("title") or "") + "\n" + (p.get("text") or "") for _, p in good]
        kw_pages = [page_score(goal, p.get("text") or "") for _, p in good]
        pscores, pvecs = await scorer.score_pages(state.goal, texts, kw_pages)
        to_store: list[dict] = []
        for (item, page), pscore, pvec in zip(good, pscores, pvecs):
            url, depth = item[2], int(item[3])
            text = page.get("text") or ""
            title = page.get("title") or url
            final = page.get("url") or url
            state._seen_set().add(canonical(final))
            links = page.get("links")
            if links is None:
                links = extract_links(page.get("raw_html") or page.get("html") or "", final)
            followed = 0
            if depth < state.max_depth:
                cands = [(lk, an) for lk, an in links if not (state.same_site and _host(lk) not in state.seed_hosts)]
                cands = [(lk, an) for lk, an in cands if canonical(lk) not in state._seen_set()]
                kw = [score_link(goal, lk, an, parent_score=pscore, same_host=_host(lk) == _host(final)) for lk, an in cands]
                for (lk, an), s in zip(cands, await scorer.score_links(state.goal, cands, kw)):
                    if s >= min_link_score and state.push(lk, depth + 1, s, pscore, an):
                        followed += 1
            note = summarize(goal, title, text)
            from_memory = bool(page.get("from_memory"))
            if from_memory:
                state.reused += 1
            n = asdict(Note(final, title[:200], depth, pscore, note, followed))
            if from_memory:
                n["from_memory"] = True
                n["fetched_at"] = page.get("fetched_at") or ""
            state.notes.append(n)
            if store is not None and not from_memory:
                to_store.append(
                    {"canonical_url": canonical(final), "url": final, "title": title, "text": text, "note": note,
                     "links": list(links)[:200], "goal": state.goal, "embedding": pvec}
                )
        if to_store:
            await store.put_pages(to_store)
        state.updated_at = time.time()
        if save is not None:
            await save(state)
        if report is not None:
            await report(f"Read {state.pages} pages, {len(state.notes)} notes, {len(state.frontier)} links queued")
    state.status = "done" if state.stop_reason in {"no promising links left", "page budget reached"} or state.stop_reason.startswith("enough") else "paused"
    state.updated_at = time.time()
    if save is not None:
        await save(state)
    return state


def new_state(goal: str, seeds: list[str], *, max_pages: int, max_depth: int, same_site: bool) -> CrawlState:
    state = CrawlState(
        id=secrets.token_hex(6),
        goal=goal,
        max_pages=max(1, min(MAX_PAGES_CAP, int(max_pages))),
        max_depth=max(0, min(5, int(max_depth))),
        same_site=same_site,
    )
    for url in seeds:
        if state.push(url, 0, 1.0):
            state.seed_hosts.append(_host(url))
    return state


def digest(state: CrawlState, top: int = 15, query: str = "", conflicts: list[dict] | None = None) -> dict:
    """Compact view for the model: best notes first, conflicting claims with both sources, what needs the browser."""
    from app.agent.contradictions import find_contradictions

    notes = list(state.notes)
    if query:
        q = terms(query)
        notes.sort(key=lambda n: -page_score(q, n["note"]))
    else:
        notes.sort(key=lambda n: -n["score"])
    if conflicts is None:
        conflicts = find_contradictions(state.notes)
    return {
        "crawl_id": state.id,
        "goal": state.goal,
        "status": state.status,
        "stop_reason": state.stop_reason,
        "pages_read": state.pages,
        "reused_from_memory": state.reused,
        "scoring": state.scoring,
        "notes_total": len(state.notes),
        "queued_links": len(state.frontier),
        "notes": notes[: max(1, top)],
        "conflicts": conflicts,
        "needs_browser": state.browser[:20],
        "next": (
            "Synthesize from the notes; where 'conflicts' lists disagreeing sources, say so and cite both (or check "
            "a primary source). Call research_notes for more or a narrower query. Open needs_browser URLs with "
            "browser_new_tab only if they matter. Resume with research_crawl(crawl_id=...) for more pages."
        ),
    }


def _summary_text(state: CrawlState, top: int = 8) -> str:
    best = sorted(state.notes, key=lambda n: -n["score"])[:top]
    return "\n".join(f"- {n['title']}: {n['note'][:300]} ({n['url']})" for n in best)


async def _conflicts(state: CrawlState, scorer: Any) -> list[dict]:
    from app.agent.contradictions import find_contradictions, llm_contradictions

    found = find_contradictions(state.notes)
    if getattr(scorer, "llm", None) is not None and len(state.notes) >= 2:
        best = sorted(state.notes, key=lambda n: -n["score"])[:12]
        found = (await llm_contradictions(best, state.goal, scorer.llm)) + found
    return found[:10]


# Redis checkpoints -------------------------------------------------------------------------------------------


def _key(user_id: Any, crawl_id: str) -> str:
    return f"research:{user_id}:{crawl_id}"


async def save_state(user_id: Any, state: CrawlState, redis_client: Any = None) -> None:
    try:
        if redis_client is None:
            from app.cache import get_redis

            redis_client = get_redis()
        await redis_client.set(_key(user_id, state.id), state.to_json(), ex=STATE_TTL_S)
    except Exception as exc:  # noqa: BLE001 - checkpointing is best effort; the crawl keeps going
        log.warning("research checkpoint failed: %s", type(exc).__name__)


async def load_state(user_id: Any, crawl_id: str, redis_client: Any = None) -> CrawlState | None:
    if not re.fullmatch(r"[0-9a-f]{6,32}", crawl_id or ""):
        return None
    try:
        if redis_client is None:
            from app.cache import get_redis

            redis_client = get_redis()
        raw = await redis_client.get(_key(user_id, crawl_id))
    except Exception:  # noqa: BLE001
        return None
    return CrawlState.from_json(raw) if raw else None


async def http_fetch(url: str) -> dict:
    """Plain HTTP fetch (no browser): readable text plus the raw HTML for links."""
    from app.connectors.web_fetch import fetch_public_page

    page = await fetch_public_page(url)
    return page


def _default_store(user_id: Any):
    import uuid as _uuid

    if isinstance(user_id, _uuid.UUID):
        from app.agent.research_store import DbResearchStore

        return DbResearchStore(user_id)
    return None


async def run_research_tool(
    ctx,
    goal: str,
    seeds: list[str] | None = None,
    queries: list[str] | None = None,
    max_pages: int = 30,
    max_depth: int = 2,
    same_site: bool = False,
    crawl_id: str = "",
    more_pages: int = 0,
    max_age_hours: float = 72.0,
    rerank: str = "auto",
    fetch: Fetcher | None = None,
    search: Callable[[str, int], Awaitable[dict]] | None = None,
    redis_client: Any = None,
    scorer: Any = None,
    store: Any = "auto",
) -> dict:
    """Start or resume a crawl for the agent tool; returns a compact digest."""
    from app.agent.relevance import default_scorer
    from app.connectors.daytona_box import safe_http_url

    user_id = getattr(ctx.user, "id", "anon")
    if store == "auto":
        store = _default_store(user_id)
    scorer = scorer or default_scorer(rerank)
    if crawl_id:
        state = await load_state(user_id, crawl_id, redis_client)
        if state is None:
            return {"error": f"No crawl {crawl_id} (it may have expired). Start a new one with a goal."}
        if more_pages:
            state.max_pages = min(MAX_PAGES_CAP, state.pages + max(1, int(more_pages)))
    else:
        if not (goal or "").strip():
            return {"error": "goal is required"}
        urls: list[str] = []
        for raw in seeds or []:
            try:
                urls.append(safe_http_url(str(raw)))
            except Exception:  # noqa: BLE001 - skip private / malformed URLs
                continue
        if search is None:
            from app.agent.web_tools import web_search

            async def search(q: str, n: int) -> dict:  # noqa: F811 - same order as the chat tool (free, browser, paid)
                return await web_search(q, n, ctx)
        for q in (queries or [])[:5]:
            try:
                found = await search(q, 8)
            except Exception:  # noqa: BLE001
                continue
            urls += [str(r.get("url")) for r in (found.get("results") or []) if r.get("url")]
        if not urls:
            return {"error": "Give seed URLs or search queries to start from."}
        state = new_state(goal, urls, max_pages=max_pages, max_depth=max_depth, same_site=same_site)
    state.scoring = scorer.provider

    progress = getattr(ctx, "progress", None)

    async def report(line: str) -> None:
        if progress is not None:
            try:
                await progress.emit(f"Research: {line}", tool="research_crawl")
            except Exception:  # noqa: BLE001
                pass

    async def save(st: CrawlState) -> None:
        await save_state(user_id, st, redis_client)

    async def guarded_fetch(url: str) -> dict:
        safe_http_url(url)  # never follow links into private networks
        return await (fetch or http_fetch)(url)

    await crawl(state, guarded_fetch, save=save, report=report, scorer=scorer, store=store, max_age_h=max_age_hours)
    state.scoring = scorer.provider  # may have dropped to keywords if the provider failed
    conflicts = await _conflicts(state, scorer)
    if store is not None and state.status == "done" and state.notes and not state.summary_saved:
        summary = _summary_text(state)
        vec = None
        gv = await scorer.goal_vec(state.goal)
        if gv is not None:
            vec = gv
        await store.save_summary(
            {"crawl_id": state.id, "goal": state.goal, "summary": summary, "embedding": vec,
             "job_id": getattr(ctx, "job_id", None), "sources": [n["url"] for n in state.notes[:30]]}
        )
        state.summary_saved = True
        await save_state(user_id, state, redis_client)
    return digest(state, conflicts=conflicts)


async def research_notes_tool(ctx, crawl_id: str, query: str = "", top: int = 15, redis_client: Any = None) -> dict:
    state = await load_state(getattr(ctx.user, "id", "anon"), crawl_id, redis_client)
    if state is None:
        return {"error": f"No crawl {crawl_id} (it may have expired)."}
    return digest(state, top=max(1, min(int(top or 15), 60)), query=query)


async def research_memory_search_tool(ctx, query: str, top: int = 10, store: Any = "auto", scorer: Any = None) -> dict:
    """Search this user's earlier research (pages read and crawl/job summaries)."""
    from app.agent.relevance import default_scorer

    if not (query or "").strip():
        return {"error": "query is required"}
    if store == "auto":
        store = _default_store(getattr(ctx.user, "id", None))
    if store is None:
        return {"pages": [], "summaries": [], "note": "No research memory for this user."}
    scorer = scorer or default_scorer()
    vec = None
    got = await scorer._vecs([query]) if getattr(scorer, "embed", None) else None
    if got:
        vec = got[0]
    out = await store.search(query, vec, top)
    out["mode"] = "semantic" if vec is not None else "keyword"
    out["next"] = "Reuse these notes; re-crawl a URL only if it is stale or missing what you need."
    return out


async def research_save_summary_tool(ctx, goal: str, summary: str, sources: list[str] | None = None, store: Any = "auto", scorer: Any = None) -> dict:
    """Remember a finished research conclusion so later jobs can find it."""
    from app.agent.relevance import default_scorer

    if not (summary or "").strip():
        return {"error": "summary is required"}
    if store == "auto":
        store = _default_store(getattr(ctx.user, "id", None))
    if store is None:
        return {"ok": False, "error": "No research memory for this user."}
    scorer = scorer or default_scorer()
    got = await scorer._vecs([f"{goal}\n{summary}"[:4000]]) if getattr(scorer, "embed", None) else None
    await store.save_summary(
        {"goal": goal, "summary": summary, "sources": list(sources or [])[:50], "embedding": got[0] if got else None,
         "job_id": getattr(ctx, "job_id", None)}
    )
    return {"ok": True}


async def rank_snapshot_links(goal: str, snapshot: dict, top: int = 12, scorer: Any = None) -> dict:
    """Score the link refs of a live browser snapshot against a goal, so the agent clicks or tabs the best ones."""
    from app.agent.relevance import Scorer

    scorer = scorer or Scorer(mode="keywords")
    g = terms(goal)
    page_url = str(snapshot.get("url") or "")
    rows = []
    seen: dict[str, dict] = {}
    for el in snapshot.get("elements") or []:
        href = str(el.get("href") or "")
        if not href.startswith(("http://", "https://")):
            continue
        key = canonical(href)
        if not key or key == canonical(page_url):
            continue
        if key in seen:
            seen[key]["text"] = _merge_anchor(seen[key]["text"], str(el.get("name") or ""))
            continue
        row = {"ref": el.get("ref"), "url": href, "text": str(el.get("name") or ""), "score": 0.0, "in_view": el.get("in_view")}
        seen[key] = row
        rows.append(row)
    for r in rows:
        r["score"] = score_link(g, r["url"], r["text"], same_host=_host(r["url"]) == _host(page_url))
    hybrid = await scorer.score_links(goal, [(r["url"], r["text"]) for r in rows], [r["score"] for r in rows])
    for r, s in zip(rows, hybrid):
        r["score"] = s
    rows.sort(key=lambda r: -r["score"])
    return {
        "ok": True,
        "url": page_url,
        "tab": snapshot.get("tab"),
        "scoring": scorer.provider,
        "links": rows[: max(1, top)],
        "next": "Click a ref with browser_act, or open several with browser_new_tab(url, background=true) and compare.",
    }
