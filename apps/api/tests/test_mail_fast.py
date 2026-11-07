"""Mail and calendar questions go straight to the mailbox tools: no web lookup, no rediscovery, no stray thread."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent import deep, fast_path
from app.agent.turn_context import split_prompt


def _conn(slug, provider):
    return SimpleNamespace(slug=slug, provider=provider, kind="builtin", name=slug.title(), alias="", description="", tags=[])


def test_the_mailbox_tools_are_named_in_the_cached_part_of_the_prompt():
    user = SimpleNamespace(id="u1", timezone="Asia/Kolkata", email="g@agilitas.test", display_name="Gaurav")
    agent = SimpleNamespace(id="a1", name="Chief", title="chief of staff", instructions="", focus="", kind="default")
    prompt = deep.system_prompt(user, [_conn("outlook", "outlook")], "", agent=agent, skills=[], matched_skills=[], query="x")
    fixed, _turn = split_prompt(prompt)
    assert "call_tool(connector_slug, tool_name, args) on 'outlook'" in fixed
    assert "- list_messages(limit, folder, unread_only)" in fixed and "- list_events(start, end, limit)" in fixed
    approvals = next(line for line in fixed.splitlines() if line.startswith("- Need the user's approval:"))
    assert "create_teams_meeting" in approvals and "create_google_meet" not in approvals  # Outlook gets Teams
    assert "send_message(to, subject, body, cc, bcc)" in fixed  # listed as needing approval


def test_no_mailbox_no_quick_reference():
    assert deep.mail_quick_ref([_conn("salesops", "mcp")]) == ""
    both = deep.mail_quick_ref([_conn("gmail", "gmail"), _conn("outlook", "outlook")])
    assert "'gmail' or 'outlook'" in both and "create_google_meet" in both and "create_teams_meeting" in both


@pytest.mark.parametrize(
    "query, kind",
    [
        ("Show my 5 latest emails: sender and subject only.", fast_path.DIRECT),
        ("What's on my calendar today and tomorrow?", fast_path.DIRECT),
        ("Do I have any meetings tomorrow?", fast_path.DIRECT),
        ("Summarise the latest email from Priya", fast_path.DIRECT),
        ("Open the email from Aditya about the dashboard", fast_path.DIRECT),
        ("Open gmail in the browser and log in", fast_path.COMPUTER),
        ("Open the admin dashboard on my site", fast_path.COMPUTER),
        ("What is the latest news on Tesla?", fast_path.QUICK_WEB),
        ("What is the weather today?", fast_path.QUICK_WEB),
    ],
)
def test_own_mail_and_calendar_are_not_web_lookups(query, kind):
    assert fast_path.classify_turn(query).kind == kind


@pytest.mark.parametrize(
    "query, opens",
    [
        ("Show my 5 latest emails: sender and subject only.", False),
        ("What's in my inbox?", False),
        ("Summarise my latest emails", False),
        ("List unread messages from today", False),
        ("Summarise the latest email from Priya", True),
        ("Open the email from Aditya about the dashboard", True),
        ("What does the attachment in that mail say?", True),
        ("Read me the full thread with Rahul", True),
    ],
)
def test_a_whole_thread_is_opened_only_for_one_specific_email(query, opens):
    assert deep._wants_mail_open(query) is opens
