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


from types import SimpleNamespace  # noqa: E402

from app.memory_engine.entities import first_name_of, looks_like_user  # noqa: E402

GAURAV = SimpleNamespace(display_name="Gaurav R", email="gauravr@agilitas.test")


def test_first_name():
    assert first_name_of(GAURAV) == "gaurav"
    assert first_name_of(SimpleNamespace(display_name="", email="priya.nair@acme.com")) == "priya"
    assert first_name_of(SimpleNamespace(display_name="Al", email="x@y.z")) == ""  # too short to tell anyone apart


@pytest.mark.parametrize(
    "name, email, mine",
    [
        ("gaurav grv", "gaurav.grv@gmail.com", True),
        ("Gaurav", "gauravr1990@outlook.com", True),
        ("Gaurav Sharma", "gaurav.sharma@acme.com", False),  # a colleague at a company address
        ("Gaurav Sharma", "gs1990@gmail.com", False),  # the mailbox doesn't carry the name
        ("Priya Nair", "gaurav.fan@gmail.com", False),
        ("", "gaurav@gmail.com", False),
    ],
)
def test_looks_like_the_user(name, email, mine):
    assert looks_like_user(name, email, GAURAV) is mine


@pytest.mark.parametrize(
    "name, email, room",
    [
        ("Salarpuria Tower II-Bangalore-First-Floor-1-Bangalore - 1st Floor (6)", "x@agilitas.com", True),
        ("", "conf.blr2@agilitas.com", True),
        ("Boardroom 4", "b4@agilitas.com", True),
        ("Deepak Jha", "deepak.jha@agilitas.com", False),
        ("Sam Hall", "sam.hall@acme.com", False),
    ],
)
def test_meeting_rooms_are_not_people(name, email, room):
    from app.memory_engine.learn import is_resource

    assert is_resource(name, email) is room


@pytest.mark.parametrize(
    "fact, noise",
    [
        ("Deepak Jha has 8 emails exchanged and 7 meetings noted.", True),
        ("Gourishanker Jha — 14 emails (13 sent, 1 received); 9 meetings.", True),
        ("Priya is COO at Zeta Corp", False),
        ("The user has a meeting with Priya on Friday", False),
    ],
)
def test_count_only_facts_are_noise(fact, noise):
    from app.memory_engine.consolidate import is_count_fact

    assert is_count_fact(fact) is noise
