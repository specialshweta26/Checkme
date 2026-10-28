"""Goal-directed crawl: dedupe, scoring, budgets, stop conditions, checkpoints, browser fallback."""

from __future__ import annotations

from types import SimpleNamespace

from app.agent import research as R

FILLER = " ".join(["Plain filler sentence about nothing in particular here."] * 6)


def page(url, title, body, links=()):
    anchors = "".join(f'<a href="{h}">{t}</a>' for h, t in links)
    return {"ok": True, "url": url, "title": title, "text": f"{body} {FILLER}", "html": f"<html><head><base href='{url}'></head><body>{anchors}</body></html>", "status": 200}


SITE = {
    "https://s.test/": page(
        "https://s.test/",
        "Home",
        "Welcome. Solar panel efficiency guides are on this site.",
        [("/solar-efficiency", "Solar panel efficiency explained"), ("/privacy", "Privacy"), ("/cats", "Cute cats"), ("/solar-efficiency?utm_source=x", "Solar again")],
    ),
    "https://s.test/solar-efficiency": page(
        "https://s.test/solar-efficiency",
        "Solar efficiency",
        "Solar panel efficiency depends on temperature. Monocrystalline panel efficiency reaches 22 percent.",
        [("/solar-efficiency/temperature", "Temperature and solar efficiency"), ("/", "Home")],
    ),
    "https://s.test/solar-efficiency/temperature": page(
        "https://s.test/solar-efficiency/temperature",
        "Temperature",
        "Solar panel efficiency drops about 0.4 percent per degree of heat.",
    ),
    "https://s.test/cats": page("https://s.test/cats", "Cats", "Cats are great."),
}


async def fake_fetch(url):
    key = url.split("#")[0]
    if key in SITE:
        return SITE[key]
    raise RuntimeError("404")


class FakeRedis:
    def __init__(self):
        self.data = {}

    async def set(self, k, v, ex=None):
        self.data[k] = v

    async def get(self, k):
        return self.data.get(k)


def test_canonical_dedupes_tracking_and_case():
    assert R.canonical("HTTP://WWW.S.test/a/?utm_source=x&b=1#frag") == R.canonical("https://s.test/a?b=1")
    assert R.canonical("mailto:x@y") == ""


def test_links_are_scored_by_goal_and_low_value_pages_sink():
    g = R.terms("solar panel efficiency")
    good = R.score_link(g, "https://s.test/solar-efficiency", "Solar panel efficiency explained")
    cats = R.score_link(g, "https://s.test/cats", "Cute cats")
    privacy = R.score_link(g, "https://s.test/privacy-solar", "Solar privacy policy")
    assert good > privacy > cats


async def test_best_first_crawl_follows_relevant_links_only():
    state = R.new_state("solar panel efficiency", ["https://s.test/"], max_pages=10, max_depth=3, same_site=True)
    await R.crawl(state, fake_fetch, concurrency=3)
    urls = [n["url"] for n in state.notes]
    assert "https://s.test/solar-efficiency/temperature" in urls
    assert "https://s.test/cats" not in urls and "https://s.test/privacy" not in urls
    assert len(urls) == len(set(urls)) == 3  # the utm duplicate and the link back home were deduped
    assert state.status == "done" and state.stop_reason == "no promising links left"
    best = R.digest(state)["notes"][0]
    assert "efficiency" in best["note"].lower()


async def test_page_budget_checkpoints_and_resume():
    redis = FakeRedis()
    ctx = SimpleNamespace(user=SimpleNamespace(id="u1"), progress=None)
    first = await R.run_research_tool(ctx, "solar panel efficiency", ["https://s.test/"], max_pages=1, max_depth=3, fetch=fake_fetch, redis_client=redis)
    assert first["pages_read"] == 1 and first["stop_reason"] == "page budget reached"
    more = await R.run_research_tool(ctx, "", crawl_id=first["crawl_id"], more_pages=5, fetch=fake_fetch, redis_client=redis)
    assert more["pages_read"] == 3 and more["crawl_id"] == first["crawl_id"]
    notes = await R.research_notes_tool(ctx, first["crawl_id"], query="temperature heat", redis_client=redis)
    assert notes["notes"][0]["url"].endswith("/temperature")


async def test_enough_evidence_stops_early():
    state = R.new_state("solar panel efficiency", ["https://s.test/"], max_pages=10, max_depth=3, same_site=True)
    await R.crawl(state, fake_fetch, concurrency=1, enough_relevant=1, relevant_at=0.9)
    assert state.stop_reason.startswith("enough evidence") and state.pages < 4


async def test_thin_or_failed_pages_go_to_the_browser_list():
    async def fetch(url):
        if "js" in url:
            return {"ok": True, "url": url, "title": "App", "text": "Loading...", "html": "<div id=root></div>"}
        raise TimeoutError()

    state = R.new_state("anything", ["https://js.test/", "https://down.test/"], max_pages=5, max_depth=1, same_site=False)
    await R.crawl(state, fetch)
    reasons = {b["url"]: b["reason"] for b in state.browser}
    assert "thin" in reasons["https://js.test/"] and "fetch error" in reasons["https://down.test/"]
    assert not state.notes


def test_rank_snapshot_links():
    snap = {
        "url": "https://s.test/",
        "elements": [
            {"ref": "e0", "href": "https://s.test/cats", "name": "Cats"},
            {"ref": "e1", "href": "https://s.test/solar-efficiency", "name": "Solar panel efficiency"},
            {"ref": "e2", "href": "", "name": "Search"},
        ],
    }
    out = R.rank_snapshot_links("solar efficiency", snap)
    assert [r["ref"] for r in out["links"]] == ["e1", "e0"]
