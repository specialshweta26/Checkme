from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from app.security import load_state
from app.storage import get_bytes, guess_media_type, render_pdf_preview

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
            png = render_pdf_preview(body)
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
