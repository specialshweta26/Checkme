"""A search-results link that won't open sends the agent to the site's own search form (docs/BROWSING.md "Deep links")."""

from app.agent import computer_harness as ch

DEEP = "https://www.example-travel.test/flight/search?itinerary=BOM-DGH-21/11/2026&tripType=O"


def test_a_failed_deep_link_points_at_the_home_page_and_its_form():
    for result in (
        {"ok": False, "error": "Timeout 60000ms exceeded"},
        {"ok": True, "url": "chrome-error://chromewebdata/", "text": ""},
        {"ok": True, "status": 403, "text": "Access Denied", "tree": ""},
        {"ok": True, "bot_wall": True, "text": "Verify you are human"},
        {"ok": True, "url": DEEP, "text": "", "tree": ""},
    ):
        out = ch.deep_link_fallback("browser_navigate", {"url": DEEP}, dict(result))
        assert out["deep_link_failed"] is True and out["fallback_url"] == "https://www.example-travel.test/"
        assert "search form" in out["hint"] and "filters" in out["hint"]


def test_working_pages_and_home_pages_are_left_alone():
    page = {"ok": True, "url": DEEP, "text": "Flights from Mumbai to Deoghar " * 5, "tree": "- e1 button Search"}
    assert "deep_link_failed" not in ch.deep_link_fallback("browser_navigate", {"url": DEEP}, dict(page))
    home = {"ok": False, "error": "blocked"}
    assert "deep_link_failed" not in ch.deep_link_fallback("browser_navigate", {"url": "https://www.example-travel.test/"}, dict(home))
    assert "deep_link_failed" not in ch.deep_link_fallback("browser_act", {"url": DEEP}, {"ok": False})
    assert ch.deep_link_fallback("browser_navigate", {"url": DEEP}, "text") == "text"


def test_the_prompt_tells_the_agent_to_use_the_site_itself():
    import inspect

    from app.agent import deep

    assert "deep_link_failed" in inspect.getsource(deep)
