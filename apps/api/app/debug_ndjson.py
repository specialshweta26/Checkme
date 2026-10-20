from __future__ import annotations

import json
import time
import urllib.request
from typing import Any

_PATHS = (
    "/Users/gaurav/Desktop/personal_assistant/.cursor/debug-daee2b.log",
    "/app/app/.cursor-debug-daee2b.log",
)
_INGEST = (
    "http://127.0.0.1:7755/ingest/7762fe36-1f54-4ec2-b56d-3aaac63261c6",
    "http://host.docker.internal:7755/ingest/7762fe36-1f54-4ec2-b56d-3aaac63261c6",
)


def dbg(hypothesis_id: str, location: str, message: str, data: dict[str, Any] | None = None, run_id: str = "pre-fix") -> None:
    rec = {
        "sessionId": "daee2b",
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data or {},
        "timestamp": int(time.time() * 1000),
        "runId": run_id,
    }
    line = json.dumps(rec, default=str) + "\n"
    for path in _PATHS:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception:
            pass
    body = json.dumps(rec, default=str).encode("utf-8")
    for url in _INGEST:
        try:
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json", "X-Debug-Session-Id": "daee2b"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=1.5)
        except Exception:
            pass
