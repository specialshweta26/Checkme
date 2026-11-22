"""Voice input: a recording becomes text for the message box (app/voice/routes.py)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.agent import meter
from app.config import get_settings
from app.deps import current_user
from app.voice import routes


@pytest.fixture
def client(monkeypatch):
    calls: list[dict] = []

    class Transcriptions:
        async def create(self, **kw):
            calls.append(kw)
            return SimpleNamespace(text="  draft a reply to Asha  ")

    async def not_limited(*a, **k):
        return False

    async def under_cap(db, user):
        return False

    import app.llm as llm

    monkeypatch.setattr(llm, "openai_client", lambda: SimpleNamespace(audio=SimpleNamespace(transcriptions=Transcriptions())))
    monkeypatch.setattr(routes, "rate_limited", not_limited)
    monkeypatch.setattr(meter, "over_user_cap", under_cap)
    monkeypatch.setattr(get_settings(), "openai_api_key", "sk-test")
    user = SimpleNamespace(id=uuid.uuid4(), email="u@x.com")
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    app.dependency_overrides[current_user] = lambda: user
    from app.db import get_db

    app.dependency_overrides[get_db] = lambda: None
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    c.calls, c.user = calls, user
    return c


async def test_recording_becomes_text_and_counts_as_usage(client):
    meter._pending.clear()
    r = await client.post("/api/voice/transcribe", files={"audio": ("voice", b"x" * 5000, "audio/webm;codecs=opus")},
                          data={"seconds": "7", "language": "en-IN"})  # fmt: skip
    assert r.status_code == 200 and r.json() == {"text": "draft a reply to Asha"}
    kw = client.calls[0]
    assert kw["file"][0] == "voice.webm" and kw["language"] == "en" and kw["model"]
    rows = [(k, v) for k, v in meter._pending.items() if k[0] == client.user.id]
    assert rows and rows[0][0][3] == "transcribe:seconds" and rows[0][1][4] == 7.0


async def test_bad_or_empty_recordings(client):
    r = await client.post("/api/voice/transcribe", files={"audio": ("v", b"x" * 5000, "application/pdf")})
    assert r.status_code == 415
    r = await client.post("/api/voice/transcribe", files={"audio": ("v", b"x" * 100, "audio/webm")})
    assert r.status_code == 200 and r.json() == {"text": ""} and not client.calls
    r = await client.post("/api/voice/transcribe", files={"audio": ("v", b"x" * (routes.MAX_BYTES + 1), "audio/webm")})
    assert r.status_code == 413


async def test_the_daily_cap_and_status(client, monkeypatch):
    async def over(db, user):
        return True

    monkeypatch.setattr(meter, "over_user_cap", over)
    r = await client.post("/api/voice/transcribe", files={"audio": ("v", b"x" * 5000, "audio/webm")})
    assert r.status_code == 429 and r.json()["detail"] == meter.CAP_MESSAGE
    assert (await client.get("/api/voice/status")).json() == {"enabled": True}
    monkeypatch.setattr(get_settings(), "voice_input", False)
    assert (await client.get("/api/voice/status")).json() == {"enabled": False}
