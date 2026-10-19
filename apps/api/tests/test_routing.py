from app.routing.split import explicit_slugs, first_party_tag


def test_explicit_slug():
    assert explicit_slugs("search @notion for the brief") == ["notion"]


def test_first_party():
    assert first_party_tag("what's on my calendar tomorrow")[1] == "calendar"
    assert first_party_tag("summarize the hiring thread")[0] == "thread_summary"
