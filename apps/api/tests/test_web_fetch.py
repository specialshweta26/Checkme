from app.connectors.web_fetch import browser_failed, html_to_text, wrap_html


def test_html_to_text_skips_scripts():
    title, text = html_to_text(
        "<html><head><title>CJP news</title><script>ignore()</script></head>"
        "<body><h1>Headline</h1><p>Protest continues in the city.</p></body></html>"
    )
    assert title == "CJP news"
    assert "Headline" in text
    assert "Protest continues" in text
    assert "ignore()" not in text


def test_browser_failed_detects_chrome_error():
    assert browser_failed({"ok": False, "url": "https://news.example", "error": "net::ERR_CONNECTION_RESET"})
    assert browser_failed({"ok": True, "url": "chrome-error://chromewebdata/"})
    assert not browser_failed({"ok": True, "url": "https://html.duckduckgo.com/html/?q=test", "text": "results"})


def test_wrap_html_inserts_base():
    html = wrap_html("<html><head></head><body>Hi</body></html>", "https://example.com/path")
    assert '<base href="https://example.com/path">' in html
