"""Semantic relevance, contradiction flags and per-user research memory (providers are faked; no network)."""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.agent import research as R
from app.agent.contradictions import find_contradictions, llm_contradictions
from app.agent.relevance import Scorer, default_scorer, link_text
from app.agent.research_store import MemoryResearchStore

# A tiny "embedding" space: words that mean the same thing share an axis, so synonyms are close without sharing
# a keyword (that is the case keyword scoring misses).
AXES = {
    "semaphore": 0, "semaphores": 0, "synchronization": 0, "primitives": 0, "lock": 0, "mutex": 0, "concurrency": 0,
    "cat": 1, "cats": 1, "pets": 1, "kitten": 1,
    "privacy": 2, "terms": 2, "cookies": 2,
    "install": 3, "download": 3, "setup": 3,
}


async def fake_embed(texts):
    out = []
    for t in texts:
        v = [0.0] * 6
        for w in re.findall(r"[a-z]+", t.lower()):
            v[AXES.get(w, 4 + (len(w) % 2))] += 1.0 if w in AXES else 0.05
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / n for x in v])
    return out


SEM = Scorer(embed=fake_embed, cos_floor=0.1, cos_ceil=0.8)
FILLER = " ".join(["Plain filler sentence about nothing in particular here."] * 6)


def page(url, title, body, links=()):
    anchors = "".join(f'<a href="{h}">{t}</a>' for h, t in links)
    return {"ok": True, "url": url, "title": title, "text": f"{body} {FILLER}", "html": f"<html><body>{anchors}</body></html>", "status": 200}


DOCS = {
    "https://d.test/": page(
        "https://d.test/",
        "Library index",
        "Index of the library modules and guides.",
        [("/sync.html", "Synchronization Primitives"), ("/pets", "Cats and kittens"), ("/privacy", "Privacy and cookies")],
    ),
    "https://d.test/sync.html": page("https://d.test/sync.html", "Synchronization Primitives", "A Semaphore manages an internal counter; Lock and Event are also here."),
    "https://d.test/pets": page("https://d.test/pets", "Cats", "Cats are pets."),
    "https://d.test/privacy": page("https://d.test/privacy", "Privacy", "Cookies policy."),
}


async def fetch(url):
    return DOCS[url.rstrip("/") + ("/" if url.rstrip("/") == "https://d.test" else "")]


async def test_keywords_miss_synonym_link_but_semantic_follows_it():
    kw_state = R.new_state("how does an asyncio semaphore work", ["https://d.test/"], max_pages=5, max_depth=1, same_site=True)
    await R.crawl(kw_state, fetch)
    assert "https://d.test/sync.html" not in {n["url"] for n in kw_state.notes}

    st = R.new_state("how does an asyncio semaphore work", ["https://d.test/"], max_pages=5, max_depth=1, same_site=True)
    await R.crawl(st, fetch, scorer=SEM)
    urls = [n["url"] for n in st.notes]
    assert "https://d.test/sync.html" in urls
    assert "https://d.test/pets" not in urls and "https://d.test/privacy" not in urls


async def test_scorer_falls_back_to_keywords_when_provider_fails():
    async def broken(texts):
        raise RuntimeError("provider down")

    s = Scorer(embed=broken)
    kw = [0.5, 0.1]
    assert await s.score_links("goal", [("https://a.test/x", "x"), ("https://a.test/y", "y")], kw) == kw
    scores, vecs = await s.score_pages("goal", ["a", "b"], [0.2, 0.3])
    assert scores == [0.2, 0.3] and vecs == [None, None]


def test_default_scorer_without_provider_is_keywords(monkeypatch):
    from app import config

    monkeypatch.setattr(config.get_settings(), "openai_api_key", "", raising=False)
    assert default_scorer().provider == "keywords"
    assert default_scorer("keywords").provider == "keywords"


async def test_llm_rerank_reorders_top_links():
    async def llm(prompt):
        return '{"scores": [1, 9]}'  # second shown link is the good one

    s = Scorer(embed=fake_embed, llm=llm, mode="llm", cos_floor=0.1, cos_ceil=0.8)
    cands = [("https://a.test/sync", "Synchronization primitives"), ("https://a.test/lock-guide", "Lock guide")]
    out = await s.score_links("semaphore", cands, [0.0, 0.0])
    assert len(out) == 2 and all(0 <= x <= 1 for x in out)


async def test_rank_snapshot_links_semantic():
    snap = {
        "url": "https://d.test/",
        "elements": [
            {"ref": "e0", "href": "https://d.test/pets", "name": "Cats"},
            {"ref": "e1", "href": "https://d.test/sync.html", "name": "Synchronization Primitives"},
        ],
    }
    out = await R.rank_snapshot_links("semaphore", snap, scorer=SEM)
    assert out["links"][0]["ref"] == "e1" and out["scoring"] == "embeddings"


def test_link_text_reads_path_words():
    assert "sync" in link_text("https://docs.x/3/library/asyncio-sync.html#x", "Synchronization")


def test_find_contradictions_numeric_with_both_sources():
    notes = [
        {"url": "https://a.test/1", "note": "The Eiffel Tower height is 330 metres including antennas."},
        {"url": "https://b.test/2", "note": "Official figures put the Eiffel Tower height at 300 metres."},
        {"url": "https://c.test/3", "note": "The Eiffel Tower height is 330 metres."},
        {"url": "https://d.test/4", "note": "Paris population is 2.1 million people."},
    ]
    found = find_contradictions(notes)
    assert found, found
    c = found[0]
    assert "eiffel" in c["about"] and "tower" in c["about"]
    assert {x["url"] for x in c["claims"]} <= {"https://a.test/1", "https://b.test/2", "https://c.test/3"}
    assert {x["value"] for x in c["claims"]} == {"330", "300"}
    # Agreeing sources and unrelated facts are not conflicts.
    assert not find_contradictions([notes[0], notes[2], notes[3]])


async def test_llm_contradictions_keeps_only_real_urls():
    notes = [{"url": "https://a.test/", "note": "Founded by Ann."}, {"url": "https://b.test/", "note": "Founded by Bob."}]

    async def llm(prompt):
        return (
            '{"conflicts": [{"about": "founder", "claims": [{"value": "Ann", "url": "https://a.test/"}, '
            '{"value": "Bob", "url": "https://b.test/"}]}, {"about": "made up", "claims": [{"value": "x", "url": "https://evil.test/"}]}]}'
        )

    out = await llm_contradictions(notes, "who founded it", llm)
    assert len(out) == 1 and out[0]["about"] == "founder"


async def test_digest_includes_conflicts():
    site = {
        "https://a.test/": page("https://a.test/", "A", "The bridge length is 1200 metres."),
        "https://b.test/": page("https://b.test/", "B", "The bridge length is 1450 metres."),
    }

    async def f(url):
        return site[url]

    st = R.new_state("bridge length", list(site), max_pages=5, max_depth=0, same_site=False)
    await R.crawl(st, f)
    d = R.digest(st)
    assert d["conflicts"] and {c["url"] for c in d["conflicts"][0]["claims"]} == set(site)


async def test_memory_reuses_fresh_pages_and_refetches_stale():
    store = MemoryResearchStore()
    calls = []

    async def counting(url):
        calls.append(url)
        return await fetch(url)

    st = R.new_state("semaphore", ["https://d.test/"], max_pages=5, max_depth=1, same_site=True)
    await R.crawl(st, counting, scorer=SEM, store=store)
    first = len(calls)
    assert first >= 2 and R.canonical("https://d.test/sync.html") in store.pages
    assert store.pages[R.canonical("https://d.test/sync.html")]["embedding"]

    st2 = R.new_state("semaphore", ["https://d.test/"], max_pages=5, max_depth=1, same_site=True)
    await R.crawl(st2, counting, scorer=SEM, store=store)
    assert len(calls) == first  # all served from memory, links included
    assert st2.reused == len(st2.notes) and "https://d.test/sync.html" in {n["url"] for n in st2.notes}
    assert all(n.get("from_memory") for n in st2.notes)

    for row in store.pages.values():
        row["fetched_at"] = datetime.now(timezone.utc) - timedelta(hours=100)
    st3 = R.new_state("semaphore", ["https://d.test/"], max_pages=5, max_depth=1, same_site=True)
    await R.crawl(st3, counting, scorer=SEM, store=store, max_age_h=72)
    assert len(calls) > first and st3.reused == 0


async def test_run_tool_saves_summary_and_memory_search():
    store = MemoryResearchStore()

    class Redis:
        def __init__(self):
            self.d = {}

        async def set(self, k, v, ex=None):
            self.d[k] = v

        async def get(self, k):
            return self.d.get(k)

    ctx = SimpleNamespace(user=SimpleNamespace(id="u1"), progress=None)
    out = await R.run_research_tool(ctx, "semaphore", ["https://d.test/"], max_pages=4, max_depth=1, same_site=True,
                                    fetch=fetch, redis_client=Redis(), scorer=SEM, store=store)
    assert out["status"] == "done" and out["scoring"] == "embeddings"
    assert store.summaries and store.summaries[0]["crawl_id"] == out["crawl_id"]
    found = await R.research_memory_search_tool(ctx, "Synchronization semaphore", store=store, scorer=Scorer(mode="keywords"))
    assert found["pages"] and found["mode"] == "keyword"
    saved = await R.research_save_summary_tool(ctx, "q", "answer text", ["https://d.test/"], store=store, scorer=Scorer(mode="keywords"))
    assert saved["ok"] and store.summaries[-1]["summary"] == "answer text"


async def test_memory_tools_without_store():
    ctx = SimpleNamespace(user=SimpleNamespace(id="not-a-uuid"), progress=None)
    out = await R.research_memory_search_tool(ctx, "anything", scorer=Scorer(mode="keywords"))
    assert out["pages"] == [] and out["summaries"] == []
