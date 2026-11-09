"""The desktop update feed: GitHub's release downloads fail for minutes at a time."""

import httpx
import pytest

from app.desktop import updates


FEED = {
    "version": "0.1.26",
    "platforms": {
        "darwin-aarch64": {
            "signature": "sig-a",
            "url": "https://github.com/specialshweta26/Checkme/releases/download/desktop-v0.1.26/Lumio_aarch64.app.tar.gz",
        },
        "other": {"signature": "sig-b", "url": "https://elsewhere.example/x.tar.gz"},
    },
}


def test_download_addresses_move_here_and_signatures_stay():
    out = updates.rewrite(FEED, "https://api.example")
    row = out["platforms"]["darwin-aarch64"]
    assert row["url"] == "https://api.example/api/desktop/update/asset/desktop-v0.1.26/Lumio_aarch64.app.tar.gz"
    assert row["signature"] == "sig-a"
    assert out["platforms"]["other"]["url"] == "https://elsewhere.example/x.tar.gz"


class _Client:
    answers: list = []
    urls: list = []

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        _Client.urls.append(url)
        code = _Client.answers.pop(0)
        return httpx.Response(code, content=b"data")


@pytest.fixture
def github(monkeypatch):
    _Client.urls = []
    monkeypatch.setattr(updates.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(updates, "RETRY_DELAYS", (0, 0))


async def test_a_504_is_retried(github):
    _Client.answers = [504, 504, 200]
    assert await updates.asset("desktop-v0.1.26", "Lumio_aarch64.app.tar.gz") == b"data"
    assert len(_Client.urls) == 3


async def test_a_long_outage_is_reported(github):
    _Client.answers = [504, 504, 504]
    with pytest.raises(updates.UpdateUnavailable):
        await updates.asset("desktop-v0.1.26", "Lumio_aarch64.app.tar.gz")


@pytest.mark.parametrize(
    "tag,name",
    [("main", "Lumio_x64.app.tar.gz"), ("desktop-v0.1.26", "../secrets"), ("desktop-v0.1.26", "other.tar.gz"), ("desktop-v0.1.26", "Lumio.sh")],
)
async def test_only_release_files_can_be_fetched(github, tag, name):
    with pytest.raises(LookupError):
        await updates.asset(tag, name)
    assert _Client.urls == []


async def test_an_outage_serves_the_last_good_feed(github, monkeypatch):
    monkeypatch.setattr(updates, "_feed", (0.0, FEED))
    monkeypatch.setattr(updates, "FEED_TTL_S", -1)
    _Client.answers = [504, 504, 504]
    out = await updates.latest("https://api.example")
    assert out["version"] == "0.1.26"


async def test_the_feed_points_at_the_public_website(github, monkeypatch):
    from app.config import get_settings
    from app.desktop.routes import update_feed

    monkeypatch.setattr(get_settings(), "public_web_url", "https://lumio.example")
    monkeypatch.setattr(updates, "_feed", (0.0, FEED))
    monkeypatch.setattr(updates, "FEED_TTL_S", 10**9)
    monkeypatch.setattr(updates.time, "monotonic", lambda: 1.0)
    out = await update_feed()
    assert out["platforms"]["darwin-aarch64"]["url"].startswith("https://lumio.example/api/desktop/update/asset/")
