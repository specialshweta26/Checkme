"""Voice input (docs/VOICE.md): a recording from the chat's microphone button becomes text in the message box.

The browser records (MediaRecorder: webm or mp4), posts it here, and OpenAI's transcription model turns it into text.
Nothing is sent as a message: the user reads and edits the text first. The audio isn't stored. Each recording counts
toward the user's usage as "transcribe" seconds, and the daily cap applies.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import rate_limited
from app.config import get_settings
from app.db import get_db
from app.deps import current_user
from app.models import User

log = logging.getLogger(__name__)
router = APIRouter(prefix="/voice", tags=["voice"])

MAX_BYTES = 8 * 1024 * 1024  # about 8 minutes of opus; the button stops at 2
TYPES = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "mp4", "audio/mpeg": "mp3", "audio/wav": "wav",
         "audio/x-wav": "wav", "audio/m4a": "m4a", "audio/x-m4a": "m4a", "video/webm": "webm"}  # fmt: skip


@router.get("/status")
async def status(user: User = Depends(current_user)):
    s = get_settings()
    return {"enabled": bool(getattr(s, "voice_input", True) and s.openai_api_key)}


@router.post("/transcribe")
async def transcribe(audio: UploadFile = File(...), seconds: float = Form(0), language: str = Form(""),
                     user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):  # fmt: skip
    from app.agent import meter

    s = get_settings()
    if not (getattr(s, "voice_input", True) and s.openai_api_key):
        raise HTTPException(503, "Voice input isn't set up on this server.")
    if await rate_limited(f"voice:{user.id}", 20):
        raise HTTPException(429, "Too many recordings. Wait a minute.")
    if await meter.over_user_cap(db, user):
        raise HTTPException(429, meter.CAP_MESSAGE)
    kind = (audio.content_type or "").split(";")[0].strip().lower()
    ext = TYPES.get(kind)
    if ext is None:
        raise HTTPException(415, "That recording format isn't supported.")
    data = await audio.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "That recording is too long. Keep it under two minutes.")
    if len(data) < 800:
        return {"text": ""}  # a click, not speech
    from app import llm_gateway

    lang = language.strip().lower()[:5] if language and language.strip().isascii() else ""
    try:
        out = await llm_gateway.openai_client(llm_gateway.resolve(None, "transcribe")).audio.transcriptions.create(
            model=getattr(s, "transcribe_model", "") or "gpt-4o-mini-transcribe",
            file=(f"voice.{ext}", data, kind),
            **({"language": lang.split("-")[0]} if lang else {}),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("transcription failed: %s", type(exc).__name__)
        raise HTTPException(502, "Couldn't turn that into text. Try again.")
    meter.count("transcribe:seconds", max(1.0, min(float(seconds or 0), 600.0)), user_id=user.id, feature="chat")
    return {"text": (getattr(out, "text", "") or "").strip()}
