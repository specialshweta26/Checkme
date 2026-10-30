from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.tasks import parse
from app.tasks.service import advance


@pytest.mark.asyncio
async def test_every_two_minutes_overrides_model_one_time_response(monkeypatch):
    class _Message:
        content = '{"title":"Market brief","instruction":"Summarize India market news","schedule_kind":"once","timed":true}'

    class _Choice:
        message = _Message()

    class _Response:
        choices = [_Choice()]

    async def fake_complete(*_args, **_kwargs):
        return _Response()

    monkeypatch.setattr(parse, "chat_complete", fake_complete)
    result = await parse.parse_schedule("brief me about stock market India news every 2 minutes", "Asia/Kolkata")

    assert result["schedule_kind"] == "interval"
    assert result["interval_minutes"] == 2
    assert result["schedule_label"] == "Every 2 minutes"
    assert result["next_run_at"] > datetime.now(timezone.utc)


def test_interval_schedule_advances_by_configured_period():
    now = datetime.now(timezone.utc)
    previous = now - timedelta(minutes=5)
    task = SimpleNamespace(
        schedule_kind="interval",
        interval_minutes=2,
        next_run_at=previous,
        status="running",
    )
    advance(task, "UTC")
    assert task.next_run_at > datetime.now(timezone.utc)
    assert (task.next_run_at - previous).total_seconds() % 120 == 0
    assert task.status == "active"
