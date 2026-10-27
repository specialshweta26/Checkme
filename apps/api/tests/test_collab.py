from types import SimpleNamespace

from app.agents.collab import (
    GROUP_MEMBER_MAX,
    GROUP_MEMBER_MIN,
    TEACH_MAX_SECONDS,
    parse_mentions,
    pick_owner,
    routing_prompt,
)
from app.skills.service import start_teaching


def _bot(aid, name):
    return SimpleNamespace(id=aid, name=name)


def test_group_size_is_two_to_six():
    assert GROUP_MEMBER_MIN == 2
    assert GROUP_MEMBER_MAX == 6


def test_parse_mentions_name_and_everyone():
    a = _bot("a1", "Analyst")
    r = _bot("r1", "Researcher")
    named = parse_mentions("Need this from @Analyst today", [a, r])
    assert named["everyone"] is False
    assert [m.id for m in named["agents"]] == ["a1"]
    fanout = parse_mentions("Status @everyone", [a, r])
    assert fanout["everyone"] is True
    assert fanout["agents"] == []


def test_one_owner_per_stage():
    a = _bot("a1", "Analyst")
    r = _bot("r1", "Researcher")
    group = SimpleNamespace(owner_id="r1", outcome="Ship the brief")
    assert pick_owner([a, r], group).id == "r1"
    mentioned = parse_mentions("@Analyst take the tables", [a, r])
    assert pick_owner([a, r], group, {}, mentioned).id == "a1"
    assert pick_owner([a, r], group, {"owner_id": "a1"}, {}).id == "a1"


def test_routing_prompt_covers_handoff_memory_and_computer():
    a = _bot("a1", "Analyst")
    r = _bot("r1", "Researcher")
    group = SimpleNamespace(name="Insights", outcome="Finish the weekly pack", owner_id="a1")
    text = routing_prompt(group, a, [a, r], {"everyone": True})
    lowered = text.lower()
    assert "one owner per stage" in lowered
    assert "ask_teammate" in lowered
    assert "separate per bot" in lowered
    assert "own screen" in lowered
    assert "one computer-use task" in lowered
    assert "weekly pack" in lowered
    assert "@everyone" in lowered


def test_teaching_is_ten_minutes_and_silent():
    assert TEACH_MAX_SECONDS == 600
    user = SimpleNamespace(id="u1")
    row = start_teaching(user, None)
    assert row["max_seconds"] == 600
    assert row["records_audio"] is False
    assert row["teaching"] is True
