"""The audit log (docs/ADMIN.md "Audit log"): one row per thing worth answering "who did that, and when?".

``record`` never raises and never commits on its own: the row lands with the caller's transaction, so an action that
rolls back leaves no entry. Details must never hold secrets, message bodies or tokens: ids, names and counts only.
"""

from __future__ import annotations

import csv
import io
import logging
import uuid
from typing import Any

log = logging.getLogger(__name__)


def _ip(request: Any) -> str:
    if request is None:
        return ""
    fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return (fwd or getattr(getattr(request, "client", None), "host", "") or "")[:64]


def record(db, action: str, user_id: uuid.UUID | None = None, actor_id: uuid.UUID | None = None, target: str = "",
           detail: dict | None = None, request: Any = None) -> None:  # fmt: skip
    if db is None:
        return
    try:
        from app.models import AuditEvent

        db.add(AuditEvent(user_id=user_id, actor_id=actor_id or user_id, action=action[:60], target=(target or "")[:300],
                          detail=dict(detail or {}), ip=_ip(request)))  # fmt: skip
    except Exception:  # noqa: BLE001 - the audit log never breaks the action it records
        log.warning("audit record %s failed", action, exc_info=True)


COLUMNS = ["at", "action", "user", "actor", "target", "detail", "ip"]


def to_csv(rows: list, emails: dict) -> str:
    import json

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(COLUMNS)
    for r in rows:
        cells = [r.at.isoformat(), r.action, emails.get(r.user_id, str(r.user_id or "")),
                 emails.get(r.actor_id, str(r.actor_id or "")), r.target, json.dumps(r.detail or {}, sort_keys=True), r.ip]  # fmt: skip
        # A cell starting with = + - @ is a formula in a spreadsheet: quote it so the export can't run anything.
        w.writerow(["'" + c if isinstance(c, str) and c[:1] in ("=", "+", "-", "@") else c for c in cells])
    return out.getvalue()
