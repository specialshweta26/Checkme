"""Local demo sign-in (docs/DESIGN.md "Sign-in"): offered only with DEV_LOGIN on and a local PUBLIC_WEB_URL, so a
known default account can never exist on a deployed server."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import routes
from app.config import Settings


@pytest.mark.parametrize(
    ("dev_login", "url", "enabled"),
    [
        (True, "http://localhost:3000", True),
        (True, "http://127.0.0.1:3100", True),
        (True, "http://lumio.localhost:3000", True),
        (False, "http://localhost:3000", False),
        (True, "https://checkme-web-kw68.onrender.com", False),
        (True, "https://localhost.example.com", False),
        (True, "http://localhost.evil.test", False),
    ],
)
def test_dev_login_needs_the_flag_and_a_local_web_url(dev_login, url, enabled):
    assert Settings(dev_login=dev_login, public_web_url=url).dev_login_enabled is enabled


def _client(monkeypatch, **settings):
    s = Settings(**settings)
    monkeypatch.setattr(routes, "get_settings", lambda: s)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    return TestClient(app, follow_redirects=False)


def test_production_refuses_dev_login_and_does_not_advertise_it(monkeypatch):
    client = _client(monkeypatch, dev_login=True, public_web_url="https://checkme-web-kw68.onrender.com")
    assert client.post("/api/auth/dev/login").status_code == 404
    assert client.get("/api/auth/status").json()["dev_login"] is False


def test_local_server_advertises_dev_login(monkeypatch):
    client = _client(monkeypatch, dev_login=True, public_web_url="http://localhost:3000")
    assert client.get("/api/auth/status").json()["dev_login"] is True
