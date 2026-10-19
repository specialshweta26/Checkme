from datetime import datetime, timezone

from connector_core.calendar import free_slots, parse_iso


def test_parse_graph_fraction():
    dt = parse_iso("2026-10-19T09:00:00.1234567Z")
    assert dt.tzinfo is not None


def test_free_slots_skips_weekend_and_busy():
    start = datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc)  # Monday
    end = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)
    busy = [
        (datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc), datetime(2026, 10, 19, 11, 0, tzinfo=timezone.utc)),
    ]
    slots = free_slots(busy, start, end, duration_minutes=30, tz="UTC")
    assert slots
    assert "9:00" in slots[0]["start"]
