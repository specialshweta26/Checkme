"""JSON size checks that stop as soon as the answer is known (docs/LLD.md "Big results").

A connector answer of 100,000 rows is about 20 MB of JSON. Asking "does it fit in 60,000 characters?" by encoding
all of it took about half a second each time, and a result was measured several times on its way to the model (and
once more for the read cache, which then dropped it as too big): about 2 s per call. Encoding in chunks and stopping
at the limit answers in microseconds for a big value and gives the same answer.
"""

from __future__ import annotations

import json
from typing import Any

_ENCODER = json.JSONEncoder(default=str)


def bounded_dumps(value: Any, limit: int) -> str | None:
    """``json.dumps(value)`` when it is at most ``limit`` characters, else None (without encoding the rest)."""
    parts: list[str] = []
    size = 0
    try:
        for chunk in _ENCODER.iterencode(value):
            size += len(chunk)
            if size > limit:
                return None
            parts.append(chunk)
    except (TypeError, ValueError):
        text = str(value)
        return text if len(text) <= limit else None
    return "".join(parts)


def fits(value: Any, limit: int) -> bool:
    return bounded_dumps(value, limit) is not None


def size_or_more(value: Any, cap: int = 2_000_000) -> int:
    """The JSON size, counted up to ``cap`` (a bigger value reports ``cap``): enough to say how big a result is."""
    out = bounded_dumps(value, cap)
    return len(out) if out is not None else cap
