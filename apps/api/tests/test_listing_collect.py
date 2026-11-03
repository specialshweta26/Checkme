"""Listing collection scrolls past the first viewport and keeps price fields apart."""

from pathlib import Path

from app.connectors.listing_collect import (
    EXTRACT_JS,
    REVIEW_FN,
    SCROLL_JS,
    clear_collect_cache,
    collect_page,
    review_control_ok,
    split_prices,
)


def _card(title, url, amounts):
    return {"title": title, "url": url, "amounts": amounts, "rating": None, "rating_count": None, "review_count": None}


class ListingPage:
    """Reveals a new card only after a scroll, then stops moving."""

    def __init__(self):
        self.scrolls = 0
        self.url = "https://shop.example/search"
        self.visits = []
        self.views = {
            0: [
                _card("Aerostep Slip-On", "https://shop.example/a", [{"value": 4999, "struck": True}, {"value": 3499, "struck": False}]),
                _card("Runner", "https://shop.example/b", [{"value": 2249, "struck": False}]),
                _card("Mystery", "https://shop.example/d", []),
            ],
            1: [
                _card("Aerostep Court", "https://shop.example/c", [{"value": 4100, "struck": False}]),
                _card("Aerostep Slip-On", "https://shop.example/a", [{"value": 4999, "struck": True}, {"value": 3499, "struck": False}]),
                _card("Textile Court", "https://shop.example/e", [{"value": 3600, "struck": False}]),
            ],
        }

    def evaluate(self, script):
        if "lumio-collect-cards" in script:
            return self.views.get(self.scrolls, [])
        if "lumio-collect-scroll" in script:
            self.scrolls += 1
            return {"top": self.scrolls * 100, "height": 4000, "moved": self.scrolls < 3}
        if str(script).rstrip().endswith("(false)"):
            return {
                "clicked": False,
                "reviews": ["Light and comfortable after a week"],
                "amounts": [{"value": 3499, "struck": False}, {"value": 4999, "struck": True}],
                "rating": 4.3,
                "rating_count": 601,
                "review_count": 61,
            }
        if "lumio-collect-reviews" in script:
            return {"clicked": True, "reviews": [], "amounts": []}
        return None

    def goto(self, url, **_kwargs):
        self.url = url
        self.visits.append(url)


def test_prices_keep_struck_amount_as_mrp():
    selling, mrp, other = split_prices([{"value": 4999, "struck": True}, {"value": 3499, "struck": False}])
    assert selling == 3499
    assert mrp == 4999
    assert other == []
    only, no_mrp, _rest = split_prices([{"value": 4100, "struck": False}])
    assert only == 4100 and no_mrp is None


def test_review_control_skips_purchase_actions():
    assert review_control_ok("Reviews")
    assert review_control_ok("Customer ratings")
    assert not review_control_ok("Add to cart")
    assert not review_control_ok("Pay now")
    assert not review_control_ok("Buy")


def test_scroll_collects_past_the_first_viewport_and_continues_reviews():
    clear_collect_cache()
    page = ListingPage()
    first = collect_page(page, min_price=3000, max_price=5000, words="aerostep", with_reviews=True, review_batch=1)
    assert page.scrolls >= 3
    assert first["stopped"] == "end"
    assert first["scanned"] == 5
    assert first["unpriced"] == 1
    assert first["matched"] == 2
    assert first["next_offset"] == 1
    assert [row["url"] for row in first["rows"]] == ["https://shop.example/a"]
    row = first["rows"][0]
    assert row["selling_price"] == 3499
    assert row["mrp"] == 4999
    assert row["reviews"] == ["Light and comfortable after a week"]
    assert row["reviews_complete"] is False
    assert page.visits == ["https://shop.example/a"]
    scrolls = page.scrolls
    second = collect_page(page, min_price=3000, max_price=5000, words="aerostep", with_reviews=True, review_batch=1, offset=1)
    assert page.scrolls == scrolls
    assert second["next_offset"] is None
    assert [row["url"] for row in second["rows"]] == ["https://shop.example/c"]
    assert page.visits == ["https://shop.example/a", "https://shop.example/c"]
    urls = [row["url"] for row in first["rows"] + second["rows"]]
    assert urls.count("https://shop.example/a") == 1
    assert "https://shop.example/b" not in urls
    assert "https://shop.example/e" not in urls


def test_desktop_collector_scripts_match():
    text = Path(__file__).resolve().parents[2].joinpath("desktop/src-tauri/src/collect.js").read_text()

    def part(marker):
        key = f"/* {marker} */"
        body = text.split(key, 1)[1]
        return body.split("/* lumio-collect-end */", 1)[0].strip()

    assert part("lumio-collect-cards") == EXTRACT_JS.strip()
    assert part("lumio-collect-scroll") == SCROLL_JS.strip()
    assert part("lumio-collect-review-fn") == REVIEW_FN.strip()
