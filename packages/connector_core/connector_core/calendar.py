from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Kolkata"
_FRACTION = re.compile(r"(\.\d{6})\d+")

_TZ_ALIASES = {
    "ist": DEFAULT_TZ,
    "india": DEFAULT_TZ,
    "india standard time": DEFAULT_TZ,
    "asia/calcutta": DEFAULT_TZ,
    "utc": "UTC",
    "gmt": "UTC",
}

# Graph Prefer / event payloads still want Windows timezone ids.
_GRAPH_WINDOWS = {
    DEFAULT_TZ: "India Standard Time",
    "Asia/Calcutta": "India Standard Time",
}


def zoneinfo(name: str | None = None) -> ZoneInfo:
    raw = (name or DEFAULT_TZ).strip()
    mapped = _TZ_ALIASES.get(raw.lower(), raw)
    try:
        return ZoneInfo(mapped)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def iana_tz(name: str | None = None) -> str:
    return zoneinfo(name).key


def graph_timezone(name: str | None = None) -> str:
    key = iana_tz(name)
    return _GRAPH_WINDOWS.get(key, key)


def tz_label(name: str | None = None) -> str:
    return "IST" if iana_tz(name) == DEFAULT_TZ else iana_tz(name)


def now_local(tz: str | None = None) -> datetime:
    return datetime.now(zoneinfo(tz))


def parse_iso(value: str, default_tz: str | None = None) -> datetime:
    # Graph returns 7 fractional digits; Python accepts at most 6.
    dt = datetime.fromisoformat(_FRACTION.sub(r"\1", (value or "").replace("Z", "+00:00")))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zoneinfo(default_tz))
    return dt


def as_local(value: str | datetime, tz: str | None = None) -> datetime:
    dt = value if isinstance(value, datetime) else parse_iso(value, tz)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zoneinfo(tz))
    return dt.astimezone(zoneinfo(tz))


def as_utc(value: str | datetime, default_tz: str | None = None) -> datetime:
    return as_local(value, default_tz).astimezone(timezone.utc)


def graph_datetime(dt: datetime, tz: str | None = None) -> str:
    """Naive local wall-clock for Microsoft Graph (7 fractional digits)."""
    return as_local(dt, tz).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S.0000000")


def local_wall(dt: datetime, tz: str | None = None) -> str:
    return as_local(dt, tz).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")


def format_local(value: str | datetime | None, tz: str | None = None) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        try:
            dt = parse_iso(text, tz)
        except Exception:
            try:
                dt = parsedate_to_datetime(text)
            except Exception:
                return text
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zoneinfo(tz))
    local = dt.astimezone(zoneinfo(tz))
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local.day} {local.strftime('%b %Y')}, {hour}:{local.strftime('%M')} {local.strftime('%p')} {tz_label(tz)}"


def format_slots(slots: list[dict[str, str]], tz: str | None = None) -> list[dict[str, str]]:
    return [{"start": format_local(s.get("start"), tz), "end": format_local(s.get("end"), tz)} for s in slots]


def free_slots(
    busy: list[tuple[datetime, datetime]],
    start: datetime,
    end: datetime,
    duration_minutes: int = 30,
    work_hours_start: str = "09:00",
    work_hours_end: str = "18:00",
    tz: str = DEFAULT_TZ,
    max_slots: int = 10,
) -> list[dict[str, str]]:
    zone = zoneinfo(tz)
    need = timedelta(minutes=duration_minutes)
    ws, we = time.fromisoformat(work_hours_start), time.fromisoformat(work_hours_end)
    merged: list[tuple[datetime, datetime]] = []
    for b_start, b_end in sorted(busy):
        if merged and b_start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b_end))
        else:
            merged.append((b_start, b_end))

    slots: list[dict[str, str]] = []
    day = start.astimezone(zone).date()
    last_day = end.astimezone(zone).date()
    while day <= last_day and len(slots) < max_slots:
        if day.weekday() < 5:
            window_start = max(datetime.combine(day, ws, zone), start)
            window_end = min(datetime.combine(day, we, zone), end)
            cursor = window_start
            for b_start, b_end in merged:
                if b_end <= cursor or b_start >= window_end:
                    continue
                if b_start - cursor >= need:
                    slots.append({"start": format_local(cursor, tz), "end": format_local(b_start, tz)})
                cursor = max(cursor, b_end)
            if window_end - cursor >= need:
                slots.append({"start": format_local(cursor, tz), "end": format_local(window_end, tz)})
        day += timedelta(days=1)
    return slots[:max_slots]
