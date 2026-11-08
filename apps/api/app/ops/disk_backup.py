"""One-time: copy every file on the API's old attached disk (STORAGE_DISK_PATH) to object storage (R2) under the same
key, so the disk can be removed without losing anything, and log what was there.

Runs at startup only when DISK_BACKUP is on. A file already in R2 under that key is left alone (never overwritten).
Keys are the disk's relative paths, the same keys the app used when FILE_STORAGE was "disk", so old links keep working.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


def _exists_in_r2(client, bucket: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:  # noqa: BLE001 - 404 (or anything else): treat as missing
        return False


def backup(root: Path, attachments: Path | None = None) -> dict:
    """``attachments`` (STORAGE_DISK_PATH) keeps its keys (relative to it, as the app used them); every other file
    under ``root`` goes to R2 under "disk-backup/<path relative to root>"."""
    from app.config import get_settings
    from app.storage import _client

    s = get_settings()
    out = {"files": 0, "bytes": 0, "copied": 0, "already": 0, "failed": 0, "oldest": "", "newest": "", "top": {}}
    if not root.exists():
        out["missing"] = True
        return out
    client = _client()
    dates = []
    for path in root.rglob("*"):
        if not path.is_file() or path.name.endswith(".ctype"):
            continue
        if attachments is not None and attachments in path.parents:
            rel = str(path.relative_to(attachments))
        else:
            rel = "disk-backup/" + str(path.relative_to(root))
        st = path.stat()
        out["files"] += 1
        out["bytes"] += st.st_size
        top = rel.split("/", 1)[0]
        out["top"][top] = out["top"].get(top, 0) + 1
        dates.append(datetime.fromtimestamp(st.st_mtime, timezone.utc).date().isoformat())
        if _exists_in_r2(client, s.s3_bucket, rel):
            out["already"] += 1
            continue
        ctype_file = path.with_name(path.name + ".ctype")
        ctype = ctype_file.read_text(encoding="utf-8").strip() if ctype_file.exists() else ""
        try:
            with open(path, "rb") as f:
                client.upload_fileobj(f, s.s3_bucket, rel, ExtraArgs={"ContentType": ctype or "application/octet-stream"})
            out["copied"] += 1
        except Exception:  # noqa: BLE001
            out["failed"] += 1
            log.warning("disk backup: %s not copied", rel, exc_info=True)
    if dates:
        out["oldest"], out["newest"] = min(dates), max(dates)
    out["top"] = dict(sorted(out["top"].items(), key=lambda kv: -kv[1])[:5])  # folders (user ids) with most files
    return out


async def run_once() -> None:
    from app.config import get_settings

    attachments = Path(get_settings().storage_disk_path)  # /var/data/attachments: keys as the app used them
    root = attachments.parent  # /var/data
    log.info("disk backup: starting for %s", root)
    try:
        result = await asyncio.to_thread(backup, root, attachments)
        log.info("disk backup: done %s", result)
    except Exception:  # noqa: BLE001
        log.exception("disk backup failed")
