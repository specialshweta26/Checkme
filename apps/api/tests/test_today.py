from datetime import datetime, timezone

from app.today.board import assemble

NOW = datetime(2026, 11, 13, 12, 15, tzinfo=timezone.utc)  # 5:45 PM IST
TZ = "Asia/Kolkata"


def _agents():
    return [
        {"id": "chief", "name": "Chief", "title": "Chief of staff", "color": "#f97316", "avatar": "mascot:01", "kind": "default"},
        {"id": "sales", "name": "Sales & Revenue Pulse", "title": "Sales", "color": "#22c55e", "avatar": "mascot:03", "kind": "custom", "unread": True},
        {"id": "inbox", "name": "Inbox Triage", "title": "Mail", "color": "#38bdf8", "avatar": "mascot:02", "kind": "custom", "focus": "inbox"},
    ]


def test_today_splits_replies_work_finished_and_routines():
    board = assemble(
        now=NOW,
        tz=TZ,
        name="Gaurav Shah",
        agents=_agents(),
        jobs=[
            {
                "id": "j-wait",
                "agent_id": "sales",
                "status": "waiting_input",
                "title": "Sales insights",
                "question": {"question": "For the sales insights, should I use which source?"},
                "pending": [],
                "session_id": "s1",
                "created_at": "2026-11-13T12:10:00+00:00",
            },
            {
                "id": "j-run",
                "agent_id": "chief",
                "status": "running",
                "title": "Check with the other agents",
                "session_id": "s2",
                "started_at": "2026-11-13T12:00:00+00:00",
            },
            {
                "id": "j-done",
                "agent_id": "chief",
                "status": "done",
                "title": "Check with the other agents and tell me",
                "finished_at": "2026-11-13T11:40:00+00:00",
                "session_id": "s2",
            },
            {
                "id": "j-old",
                "agent_id": "chief",
                "status": "done",
                "title": "Yesterday",
                "finished_at": "2026-11-12T11:40:00+00:00",
            },
            {
                "id": "j-stale",
                "status": "running",
                "title": "Build grocery webapp MVP",
                "agent_name": "Lumio",
                "started_at": "2026-10-28T12:00:00+00:00",
            },
            {
                "id": "j-anon",
                "status": "waiting_approval",
                "title": "Push the branch",
                "agent_name": "",
                "pending": [{"title": "Push the branch", "preview": "{'repo': 'Checkme', 'branch': 'agent/port'}"}],
                "created_at": "2026-11-13T08:00:00+00:00",
            },
        ],
        tasks=[
            {
                "id": "t-next",
                "agent_id": "inbox",
                "agent_name": "Inbox Triage",
                "title": "Inbox triage",
                "status": "active",
                "schedule_label": "Every day · 8:00 AM",
                "next_run_at": "2026-11-14T02:30:00+00:00",
            },
            {
                "id": "t-done",
                "agent_id": "chief",
                "agent_name": "Chief",
                "title": "Morning brief",
                "status": "active",
                "last_run_at": "2026-11-13T02:30:00+00:00",
                "last_result": "Morning brief is ready",
                "next_run_at": "2026-11-14T02:30:00+00:00",
                "schedule_label": "Every day · 8:00 AM",
            },
            {
                "id": "t-paused",
                "agent_id": "sales",
                "title": "Paused pulse",
                "status": "paused",
                "next_run_at": "2026-11-14T03:00:00+00:00",
            },
        ],
        replies=[
            {
                "agent_id": "sales",
                "session_id": "s1",
                "content": "For the sales insights, should I use which source?",
                "created_at": "2026-11-13T12:12:00+00:00",
            }
        ],
        alerts=[
            {
                "id": "m1",
                "subject": "Contract needs a reply",
                "alert_label": "Urgent",
                "sort_at": "2026-11-13T10:00:00+00:00",
            }
        ],
        live=[{"id": "9", "agent_id": "inbox", "session_id": "s3", "detail": "Working on a reply", "created_at": "2026-11-13T12:14:00+00:00"}],
    )

    assert board["greeting"] == "Good evening"
    assert board["name"] == "Gaurav"
    assert board["date_label"] == "Friday, November 13"
    assert board["default_agent_id"] == "chief"
    assert [a["id"] for a in board["agents"]][0] == "chief"

    need_ids = [row["id"] for row in board["needs_you"]]
    assert "job:j-wait" in need_ids
    assert "mail:m1" in need_ids
    assert any(row["title"] == "Inbox Triage" and "Contract needs a reply" in row["detail"] for row in board["needs_you"])
    assert any("Has a question for you" in row["detail"] for row in board["needs_you"] if row["id"] == "job:j-wait")

    work_agents = {row["agent_name"] for row in board["working"]}
    assert work_agents == {"Chief", "Inbox Triage"}
    assert "Build grocery webapp MVP" not in {row["detail"] for row in board["working"]}
    assert all(row["agent_name"] != "Lumio" and row["title"] != "Lumio" for row in board["needs_you"] + board["working"])
    anon = next(row for row in board["needs_you"] if row["id"] == "job:j-anon")
    assert anon["agent_name"] == "Chief"
    assert "Checkme" not in anon["detail"]
    assert "Push the branch" in anon["detail"]
    assert any(row["detail"] == "Finished · Morning brief" and row["agent_name"] == "Chief" for row in board["needs_you"])

    done_titles = [row["title"] for row in board["done_today"]]
    assert "Check with the other agents and tell me" in done_titles
    assert "Morning brief" in done_titles
    assert "Yesterday" not in done_titles
    assert all(row["id"] != "task:t-done" or "Morning" in row["title"] for row in board["done_today"])

    coming = {row["title"]: row["when"] for row in board["coming_up"]}
    assert "Paused pulse" not in coming
    assert coming["Inbox triage"] == "8:00 AM"
    assert board["counts"]["coming_up"] == 2
    assert board["counts"]["working"] == 2
