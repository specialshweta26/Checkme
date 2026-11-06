"""Files the user attached in chat. They're stored with the chat, not on any computer, so the agent reads them here
or copies them onto the computer when asked to open them there."""

from __future__ import annotations

import asyncio
import posixpath
from typing import Any

MAX_TEXT = 30000


def _owned(ctx: Any, key: str) -> bool:
    """Keys come from the model, so only this user's own uploads are allowed."""
    user_id = getattr(getattr(ctx, "user", None), "id", None)
    key = (key or "").strip()
    return bool(user_id) and key.startswith(f"{user_id}/") and ".." not in key.split("/")


def _filename(key: str) -> str:
    return key.rsplit("/", 1)[-1] or "file"


async def _load(key: str) -> bytes:
    from app.storage import get_bytes

    data, _ = await asyncio.to_thread(get_bytes, key)
    return data


async def read_upload(ctx: Any, key: str) -> dict:
    if not _owned(ctx, key):
        return {"ok": False, "error": "That attachment isn't one of this user's uploads."}
    from app.storage import extract_text

    try:
        data = await _load(key)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"The upload couldn't be loaded ({type(exc).__name__})."}
    name = _filename(key)
    text = await asyncio.to_thread(extract_text, name, "", data, MAX_TEXT)
    if not text and b"\x00" not in data[:4096]:
        text = data[:MAX_TEXT].decode("utf-8", "replace")
    if not text:
        return {"ok": False, "filename": name, "bytes": len(data), "error": "This file type's text can't be read. Copy it to the computer with save_upload_to_computer if the user wants it opened there."}
    return {"ok": True, "filename": name, "bytes": len(data), "content": text}


async def save_upload_to_computer(ctx: Any, key: str, path: str) -> dict:
    if not _owned(ctx, key):
        return {"ok": False, "error": "That attachment isn't one of this user's uploads."}
    path = (path or "").strip()
    if not path:
        return {"ok": False, "error": "path is required"}
    if path.endswith("/"):
        path = posixpath.join(path, _filename(key))
    from app.agent import computer_harness as computer

    try:
        data = await _load(key)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"The upload couldn't be loaded ({type(exc).__name__})."}
    result = await computer.write_bytes(ctx, path, data)
    if isinstance(result, dict):
        result.setdefault("path", path)
    return result if isinstance(result, dict) else {"ok": True, "path": path}
