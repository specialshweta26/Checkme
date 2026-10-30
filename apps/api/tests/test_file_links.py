from app.security import file_link_ttl_s, load_file_token, load_state, sign_file_token, sign_state
from app.storage import signed_url


def test_file_tokens_survive_far_longer_than_oauth_state():
    payload = {"key": "u/a/report.html", "filename": "report.html", "content_type": "text/html", "inline": True}
    token = sign_file_token(payload)
    loaded = load_file_token(token)
    assert loaded["key"] == "u/a/report.html"
    assert loaded["inline"] is True
    assert load_state(token) is None
    assert file_link_ttl_s() >= 30 * 86400


def test_old_oauth_signed_file_urls_still_open_with_file_loader():
    payload = {"key": "u/a/old.pdf", "filename": "old.pdf", "content_type": "application/pdf"}
    legacy = sign_state(payload)
    assert load_file_token(legacy)["key"] == "u/a/old.pdf"


def test_signed_url_uses_file_token_not_oauth_state():
    url = signed_url("u/a/x.html", "x.html", inline=True, content_type="text/html")
    assert "/api/files?t=" in url
    token = url.split("t=", 1)[1]
    assert load_file_token(token)["filename"] == "x.html"
    assert load_state(token) is None
