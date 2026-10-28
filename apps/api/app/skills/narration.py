"""Spoken narration recorded while teaching: validate, transcribe, and line it up with the actions.

The browser records the microphone (webm/opus on Chrome and Android, audio/mp4 on iOS Safari) in one or
more clips; each clip says when it started (ms since the teaching session started). Clips are transcribed
with the app's OpenAI key. whisper-1 returns timed segments; other models give one untimed segment.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config import get_settings

log = logging.getLogger(__name__)

TRANSCRIBE_TIMEOUT = 90.0
# MIME type (parameters stripped) -> file extension OpenAI accepts.
AUDIO_TYPES = {
    "audio/webm": "webm",
    "video/webm": "webm",
    "audio/mp4": "mp4",
    "video/mp4": "mp4",
    "audio/x-m4a": "m4a",
    "audio/m4a": "m4a",
    "audio/aac": "m4a",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/flac": "flac",
}


class AudioError(ValueError):
    pass


def audio_extension(content_type: str, filename: str = "") -> str:
    base = (content_type or "").split(";", 1)[0].strip().lower()
    if base in AUDIO_TYPES:
        return AUDIO_TYPES[base]
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    if ext in set(AUDIO_TYPES.values()):
        return ext
    raise AudioError(f"Unsupported audio format: {base or ext or 'unknown'}. Use webm, mp4/m4a, mp3, ogg or wav.")


def check_audio(data: bytes, content_type: str, filename: str = "") -> str:
    limit = int(get_settings().teach_audio_max_bytes or 0) or 24 * 1024 * 1024
    if not data:
        raise AudioError("The audio clip is empty.")
    if len(data) > limit:
        raise AudioError(f"The audio clip is too large (max {limit // (1024 * 1024)} MB).")
    return audio_extension(content_type, filename)


def _segments(resp: Any) -> list[dict]:
    raw = getattr(resp, "segments", None)
    if raw is None and isinstance(resp, dict):
        raw = resp.get("segments")
    out: list[dict] = []
    for seg in raw or []:
        get = seg.get if isinstance(seg, dict) else (lambda k, s=seg: getattr(s, k, None))
        text = " ".join(str(get("text") or "").split())
        if text:
            out.append({"start": float(get("start") or 0), "end": float(get("end") or 0), "text": text})
    if out:
        return out
    text = getattr(resp, "text", None) if not isinstance(resp, dict) else resp.get("text")
    if isinstance(resp, str):
        text = resp
    text = " ".join(str(text or "").split())
    return [{"start": 0.0, "end": 0.0, "text": text}] if text else []


async def transcribe(data: bytes, ext: str) -> list[dict]:
    """[{start, end, text}] in seconds from the clip start. Raises on failure."""
    from app.llm import openai_client

    model = (get_settings().openai_transcribe_model or "whisper-1").strip()
    upload = (f"narration.{ext}", data)
    client = openai_client()
    if model == "whisper-1":
        resp = await asyncio.wait_for(
            client.audio.transcriptions.create(
                model=model, file=upload, response_format="verbose_json", timestamp_granularities=["segment"]
            ),
            timeout=TRANSCRIBE_TIMEOUT,
        )
    else:
        resp = await asyncio.wait_for(
            client.audio.transcriptions.create(model=model, file=upload, response_format="json"),
            timeout=TRANSCRIBE_TIMEOUT,
        )
    return _segments(resp)


def place(segments: list[dict], offset_ms: int) -> list[dict]:
    """Clip-relative segments -> session-timed 'say' events."""
    base = max(0, int(offset_ms or 0)) / 1000.0
    return [
        {"t": round(base + s["start"], 2), "t_end": round(base + max(s["end"], s["start"]), 2), "kind": "say", "text": s["text"]}
        for s in segments
        if s.get("text")
    ]


def align(actions: list[dict], speech: list[dict], window: float = 8.0) -> list[dict]:
    """Attach to each action what the user said just before or while doing it (within ``window`` seconds)."""
    out = []
    for act in actions:
        t = float(act.get("t") or 0)
        said = [s["text"] for s in speech if s["t"] - 1.0 <= t <= s.get("t_end", s["t"]) + window and s["t"] <= t + 1.0]
        row = dict(act)
        if said:
            row["said"] = " ".join(said[-2:])[:400]
        out.append(row)
    return out


def merge(*streams: list[dict]) -> list[dict]:
    rows = [r for stream in streams for r in stream]
    rows.sort(key=lambda r: float(r.get("t") or 0))
    return rows


def clock(t: float) -> str:
    t = max(0, int(t))
    return f"{t // 60:02d}:{t % 60:02d}"


def describe(ev: dict) -> str:
    kind = ev.get("kind")
    where = f" in “{ev['window']}”" if ev.get("window") else ""
    if kind == "say":
        return f"said: “{ev.get('text', '')}”"
    if kind == "click":
        return f"clicked at ({ev.get('x')}, {ev.get('y')}){where}"
    if kind == "type":
        return f"typed “{ev.get('text', '')}”"
    if kind == "key":
        return f"pressed {ev.get('key', '')}"
    if kind == "scroll":
        return f"scrolled {ev.get('direction', 'down')} ×{ev.get('amount', 1)}"
    if kind == "window":
        return f"focused window “{ev.get('window', '')}”"
    if kind == "tool":
        return f"agent tool {ev.get('tool')} {str(ev.get('args') or '')[:160]}"
    if kind == "input":
        if ev.get("text"):
            return f"typed “{ev['text']}” (typing bar)"
        return f"pressed {ev.get('key', '')} (typing bar)"
    if kind == "control":
        return f"{ev.get('action', 'control')} {str({k: v for k, v in ev.items() if k in ('x', 'y', 'text', 'key', 'dx', 'dy') and v not in (None, '')})}"
    return str(kind)


def timeline_text(events: list[dict], limit: int = 600) -> str:
    return "\n".join(f"[{clock(float(e.get('t') or 0))}] {describe(e)}" for e in events[:limit])
