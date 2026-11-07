"""Sent mail is read by its everyday name on Outlook, and "who do I … most" questions get the top-people list."""

from __future__ import annotations

import pytest

from app.memory_engine.entities import _TOP_PEOPLE
from outlook_mcp.connector import well_known_folder


@pytest.mark.parametrize(
    "folder, expected",
    [("sent", "sentitems"), ("Sent Items", "sentitems"), ("inbox", "inbox"), ("", "inbox"), ("junk", "junkemail"),
     ("trash", "deleteditems"), ("SentItems", "sentitems"), ("Projects", None)],
)
def test_outlook_folder_names(folder, expected):
    assert well_known_folder(folder) == expected


@pytest.mark.parametrize(
    "query, top",
    [
        ("Who do I email and meet with the most? Top 5", True),
        ("who do I talk to most often", True),
        ("Who did I meet with most this month?", True),
        ("my top 5 contacts", True),
        ("Who are my key clients?", True),
        ("Who is Priya?", False),
        ("When did I last meet Priya?", False),
        ("What is the most important email today?", False),
    ],
)
def test_top_people_questions(query, top):
    assert bool(_TOP_PEOPLE.search(query)) is top
