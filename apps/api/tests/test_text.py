from connector_core.text import clean_body, html_to_text, strip_quoted


def test_html_to_text():
    assert "Hello" in html_to_text("<p>Hello<br>world</p>")


def test_strip_quoted():
    raw = "Thanks.\n\nOn Mon, Jan 1 Alice wrote:\n> old"
    assert "old" not in strip_quoted(raw)
    assert "Thanks" in strip_quoted(raw)


def test_clean_clips():
    out = clean_body("x" * 5000, False, limit=100)
    assert out.endswith("...[truncated]")
    assert len(out) < 130
