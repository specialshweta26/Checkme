from __future__ import annotations

import re
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response

from app.agent.attachments import is_previewable
from app.config import get_settings
from app.deps import current_user
from app.models import User
from app.security import load_state
from app.storage import extract_text, get_bytes, guess_media_type, put_bytes, render_pdf_preview, signed_url
from connector_core.text import clip

router = APIRouter(prefix="/files", tags=["files"])


@router.get("")
async def get_file(t: str = Query(..., min_length=8), render: bool = False):
    data = load_state(t, max_age=3600)
    if not data or not data.get("key"):
        raise HTTPException(404, "File link expired. Ask again in chat.")
    try:
        body, stored_type = get_bytes(data["key"])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(404, "File not found") from exc
    filename = data.get("filename") or "file"
    content_type = guess_media_type(filename, data.get("content_type") or stored_type)
    if render and content_type == "application/pdf":
        try:
            png = render_pdf_preview(body, max_pages=12)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(422, "Could not render this PDF") from exc
        return Response(
            content=png,
            media_type="image/png",
            headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"},
        )
    disposition = "inline" if data.get("inline") or render else "attachment"
    safe = quote(filename)
    return Response(
        content=body,
        media_type=content_type,
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{safe}",
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/upload")
async def upload_files(files: list[UploadFile] = File(...), user: User = Depends(current_user)):
    if not files:
        raise HTTPException(400, "No files selected")
    limit = get_settings().attachment_max_bytes
    out = []
    for upload in files[:8]:
        data = await upload.read()
        if len(data) > limit:
            raise HTTPException(400, f"{upload.filename or 'file'} is larger than the 25 MB limit")
        filename = re.sub(r"[^A-Za-z0-9._-]+", "_", upload.filename or "file")[:180] or "file"
        content_type = guess_media_type(filename, upload.content_type)
        key = f"{user.id}/{uuid.uuid4().hex}/{filename}"
        put_bytes(key, data, content_type)
        preview = is_previewable(filename, content_type)
        kind = "pdf" if "pdf" in content_type or filename.lower().endswith(".pdf") else "image" if content_type.startswith("image/") else "file"
        out.append(
            {
                "key": key,
                "filename": upload.filename or filename,
                "content_type": content_type,
                "size": len(data),
                "kind": kind,
                "download_url": signed_url(key, upload.filename or filename, inline=False, content_type=content_type),
                "preview_url": signed_url(key, upload.filename or filename, inline=True, content_type=content_type) if preview else None,
                "previewable": preview,
                "extracted_text": clip(extract_text(filename, content_type, data, limit=8000) or "", 4000),
            }
        )
    return {"files": out}
