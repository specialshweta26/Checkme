"""Teach-by-demonstration: capture parsing, narration alignment, session limit and save flow."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.skills import narration, recorder, service


def _fake_bin(tmp_path, name: str, body: str) -> None:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


XI2 = """EVENT type 15 (RawButtonPress)
    device: 2 (11)
    detail: 1
    valuators:
EVENT type 13 (RawKeyPress)
    device: 3 (12)
    detail: 50
EVENT type 13 (RawKeyPress)
    device: 3 (12)
    detail: 38
EVENT type 14 (RawKeyRelease)
    device: 3 (12)
    detail: 50
EVENT type 13 (RawKeyPress)
    device: 3 (12)
    detail: 56
EVENT type 13 (RawKeyPress)
    device: 3 (12)
    detail: 65
EVENT type 13 (RawKeyPress)
    device: 3 (12)
    detail: 36
EVENT type 15 (RawButtonPress)
    device: 2 (11)
    detail: 5
"""

XMODMAP = """keycode  36 = Return NoSymbol Return
keycode  38 = a A a A
keycode  50 = Shift_L NoSymbol Shift_L
keycode  56 = b B b B
keycode  65 = space NoSymbol space
"""


def test_capture_script_records_clicks_keys_and_scrolls(tmp_path):
    _fake_bin(tmp_path, "xinput", f"cat <<'XI'\n{XI2}XI\n")
    _fake_bin(tmp_path, "xmodmap", f"cat <<'XM'\n{XMODMAP}XM\n")
    _fake_bin(
        tmp_path,
        "xdotool",
        'case "$1" in getmouselocation) printf "X=412\\nY=230\\nSCREEN=0\\nWINDOW=1\\n";; *) echo "Flights - Chromium";; esac\n',
    )
    script = tmp_path / "capture.py"
    script.write_text(recorder.CAPTURE_SCRIPT)
    out = tmp_path / "events.jsonl"
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ.get('PATH', '')}")
    subprocess.run([sys.executable, str(script), str(out)], env=env, timeout=20, check=True)
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    kinds = [r["kind"] for r in rows]
    assert "capture_started" in kinds
    click = next(r for r in rows if r["kind"] == "click")
    assert (click["x"], click["y"], click["window"]) == (412, 230, "Flights - Chromium")
    assert "".join(r["char"] for r in rows if r["kind"] == "char") == "Ab "
    assert {"kind": "key", "key": "Return"}.items() <= next(r for r in rows if r["kind"] == "key").items()
    assert any(r["kind"] == "scroll" and r["direction"] == "down" for r in rows)

    events, notes = recorder.parse_capture(out.read_text(), offset=10.0)
    assert not notes
    typed = next(e for e in events if e["kind"] == "type")
    assert typed["text"] == "Ab "
    assert all(e["t"] >= 10.0 for e in events)
    assert [e["kind"] for e in events if e["kind"] != "window"] == ["click", "type", "key", "scroll"]


def test_parse_capture_merges_and_notes_missing_xinput():
    lines = [
        {"kind": "capture_started", "t": 0, "window": "Desktop"},
        {"kind": "capture_unavailable", "t": 0.1, "reason": "xinput is not installed"},
        {"kind": "scroll", "t": 1, "direction": "down"},
        {"kind": "scroll", "t": 1.5, "direction": "down"},
        {"kind": "window", "t": 2, "window": "Desktop"},
        {"kind": "window", "t": 3, "window": "Mail"},
    ]
    events, notes = recorder.parse_capture("\n".join(json.dumps(r) for r in lines) + "\nnot json\n", offset=2.0)
    assert notes and "xinput" in notes[0]
    scroll = next(e for e in events if e["kind"] == "scroll")
    assert scroll["amount"] == 2 and scroll["t"] == 3.0
    assert [e["window"] for e in events if e["kind"] == "window"] == ["Desktop", "Mail"]


def test_frame_hash_detects_change():
    from io import BytesIO

    from PIL import Image

    def png(color, box=None):
        img = Image.new("RGB", (1280, 900), color)
        if box:
            img.paste((0, 0, 0), box)
        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    a, jpeg = recorder.frame_hash(png((255, 255, 255)))
    b, _ = recorder.frame_hash(png((255, 255, 255), (0, 0, 640, 900)))
    assert jpeg[:2] == b"\xff\xd8"
    assert recorder.changed(None, a)
    assert not recorder.changed(a, a)
    assert recorder.changed(a, b)


def test_audio_formats_cover_safari_and_chrome():
    assert narration.audio_extension("audio/mp4") == "mp4"
    assert narration.audio_extension("audio/webm;codecs=opus") == "webm"
    assert narration.audio_extension("", "clip.m4a") == "m4a"
    with pytest.raises(narration.AudioError):
        narration.audio_extension("text/plain", "x.txt")
    with pytest.raises(narration.AudioError):
        narration.check_audio(b"", "audio/webm")


def test_audio_size_limit(monkeypatch):
    monkeypatch.setattr(narration, "get_settings", lambda: SimpleNamespace(teach_audio_max_bytes=10))
    with pytest.raises(narration.AudioError, match="too large"):
        narration.check_audio(b"x" * 11, "audio/mp4")
    assert narration.check_audio(b"x" * 10, "audio/mp4") == "mp4"


def test_segments_and_alignment():
    resp = SimpleNamespace(segments=[SimpleNamespace(start=0.0, end=2.0, text=" Open the airline site "),
                                     SimpleNamespace(start=5.0, end=6.0, text="then search")], text="")  # fmt: skip
    segs = narration._segments(resp)
    assert segs[0]["text"] == "Open the airline site"
    assert narration._segments({"text": "just text"}) == [{"start": 0.0, "end": 0.0, "text": "just text"}]
    speech = narration.place(segs, offset_ms=10_000)
    assert speech[0]["t"] == 10.0 and speech[1]["t"] == 15.0
    actions = [{"t": 11.0, "kind": "click", "x": 1, "y": 2}, {"t": 15.5, "kind": "type", "text": "BLR"}, {"t": 60, "kind": "key", "key": "Enter"}]
    aligned = narration.align(actions, speech)
    assert aligned[0]["said"] == "Open the airline site"
    assert "then search" in aligned[1]["said"]
    assert "said" not in aligned[2]
    text = narration.timeline_text(narration.merge(aligned, speech))
    assert "[00:10] said: “Open the airline site”" in text
    assert "[00:15] typed “BLR”" in text


def test_teach_limit_is_configurable(monkeypatch):
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(teach_max_seconds=120, openai_api_key=""))
    assert service.teach_limit() == 120
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(teach_max_seconds=5, openai_api_key=""))
    assert service.teach_limit() == 60


def test_rail_typing_deduped_against_screen_capture():
    screen = [{"t": 5.2, "kind": "type", "text": "hello", "source": "screen"}, {"t": 30, "kind": "type", "text": "x", "source": "screen"}]
    rail = [{"t": 5.0, "kind": "input", "text": "hello", "source": "rail"}]
    kept = service._dedupe_rail_typing(screen, rail)
    assert [e["t"] for e in kept] == [30]


async def test_full_teach_session_with_narration(monkeypatch, skill_store):
    user = SimpleNamespace(id="00000000-0000-0000-0000-0000000000aa")

    async def fake_stop(key):
        return {"events": [{"t": 3.0, "kind": "click", "x": 10, "y": 20, "window": "Chromium", "source": "screen"}], "frames": [], "notes": []}

    async def fake_transcribe(data, ext):
        assert ext == "mp4"
        return [{"start": 0.5, "end": 2.0, "text": "Click the search box"}]

    async def fake_writer(actions, frames, name_hint, transcript=None):
        assert name_hint == "Search flights"
        return {"name": "ignored", "instruction": "1. Open flights. 2. Search."}

    monkeypatch.setattr(service.recorder, "stop", fake_stop)
    monkeypatch.setattr(service.recorder, "start", lambda *a: None)
    monkeypatch.setattr(service.narration, "transcribe", fake_transcribe)
    monkeypatch.setattr(service, "_write_skill", fake_writer)
    fake_settings = SimpleNamespace(teach_max_seconds=600, openai_api_key="k", teach_audio_max_bytes=1000)
    monkeypatch.setattr(service, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(narration, "get_settings", lambda: fake_settings)

    started = await service.start_teaching(user, None)
    assert started["max_seconds"] == 600 and started["skill_id"]
    assert service.teaching_status(user)["teaching"] is True
    service.record_user_input(user.id, "input", text="Goa")
    await service.add_narration(user, b"audio-bytes", "audio/mp4", "narration.mp4", offset_ms=2000)
    with pytest.raises(service.SkillError):
        await service.add_narration(user, b"x" * 2000, "audio/mp4", "big.mp4", 0)
    out = await service.stop_teaching(None, user, "Search flights")
    assert out["kind"] == "taught" and out["narrated"] is True and out["status"] == "writing"
    await skill_store.settle()
    saved = skill_store.rows[out["id"]]
    steps = saved["steps"]
    kinds = [s["kind"] for s in steps]
    assert "say" in kinds and "click" in kinds and "input" in kinds
    click = next(s for s in steps if s["kind"] == "click")
    assert click["said"] == "Click the search box"
    assert saved["transcript"][0]["text"] == "Click the search box"
    assert saved["status"] == "ready" and saved["error"] == ""
    assert saved["instruction"] == "1. Open flights. 2. Search."
    assert saved["name"] == "Search flights"
    assert service.teaching_status(user)["teaching"] is False
    with pytest.raises(service.SkillError):
        await service.stop_teaching(None, user, "again")


def test_audio_route_accepts_multipart_and_rejects_oversize(monkeypatch):
    import uuid

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.deps import current_user
    from app.skills import routes

    seen = {}

    async def fake_add(user, data, content_type, filename, offset_ms):
        seen.update(size=len(data), type=content_type, name=filename, offset=offset_ms)
        if len(data) > 10:
            raise service.SkillError("Narration clip is too large")
        return {"ok": True, "clips": 1}

    monkeypatch.setattr(routes.service, "add_narration", fake_add)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=uuid.uuid4())
    client = TestClient(app)
    prefix = routes.router.prefix
    ok = client.post(f"{prefix}/teach/audio", files={"audio": ("n.mp4", b"abc", "audio/mp4")}, data={"offset_ms": "1500"})
    assert ok.status_code == 200 and ok.json()["clips"] == 1
    assert seen == {"size": 3, "type": "audio/mp4", "name": "n.mp4", "offset": 1500}
    big = client.post(f"{prefix}/teach/audio", files={"audio": ("n.webm", b"x" * 50, "audio/webm")})
    assert big.status_code == 413
