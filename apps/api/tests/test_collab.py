from types import SimpleNamespace

from app.agents.collab import (
    GROUP_MEMBER_MAX,
    GROUP_MEMBER_MIN,
    TEACH_MAX_SECONDS,
    creator_name,
    is_group_hello,
    is_kickoff,
    is_roll_call,
    parse_mentions,
    pick_owner,
    role_line,
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
    assert "each bot" in lowered
    assert "do not combine their replies" in lowered
    assert "then combine" not in lowered


def test_roll_call_and_kickoff_detection():
    assert is_roll_call("please each reply with a one-line hello and what you're best at")
    assert is_roll_call("introduce yourselves")
    assert not is_roll_call("pull the weekly pack")
    assert is_group_hello("Hi team")
    assert is_group_hello("hello")
    assert not is_group_hello("pull the weekly pack")
    assert is_kickoff([])
    assert is_kickoff([{"role": "user", "content": "hi"}])
    assert not is_kickoff([{"role": "assistant", "content": "hello"}])


def test_kickoff_prompt_names_creator_and_roles():
    exp = SimpleNamespace(
        id="e1",
        name="Experimental",
        title="home and money",
        instructions="Reminders, shopping, local services in Bengaluru.",
        focus="errands",
    )
    ori = SimpleNamespace(
        id="o1",
        name="Ori",
        title="dev work",
        instructions="Self-hosting, Render, GitHub, code.",
        focus="coding",
    )
    tester = SimpleNamespace(
        id="t1",
        name="Test",
        title="research and testing",
        instructions="Business data from connected dashboards.",
        focus="research",
    )
    group = SimpleNamespace(name="Master Group", outcome="Work together", owner_id="e1")
    user = SimpleNamespace(display_name="Shweta", email="s@x.com")
    text = routing_prompt(group, exp, [exp, ori, tester], {"kickoff": True, "roll_call": True}, user=user)
    lowered = text.lower()
    assert "shweta" in lowered
    assert "set this group up" in lowered
    assert "experimental" in lowered
    assert "ori" in lowered
    assert "proposed roles" in lowered
    assert "ask_teammate" in lowered
    assert "own bubble" in lowered or "only your intro" in lowered
    assert "one8" not in lowered
    assert "lotto" not in lowered
    assert "combine" not in lowered
    assert "bengaluru" in role_line(exp).lower()
    assert creator_name(user) == "Shweta"


def test_handoff_prompt_is_first_person():
    ori = SimpleNamespace(id="o1", name="Ori", title="dev work", instructions="", focus="coding")
    exp = SimpleNamespace(id="e1", name="Experimental", title="home", instructions="", focus="errands")
    group = SimpleNamespace(name="Master Group", outcome="", owner_id="e1")
    user = SimpleNamespace(display_name="Shweta")
    text = routing_prompt(group, ori, [exp, ori], {"handoff": True}, user=user)
    lowered = text.lower()
    assert "first person" in lowered
    assert "shweta" in lowered
    assert "ask_teammate" not in lowered
    assert "one8" not in lowered


def test_teaching_is_ten_minutes_with_optional_narration():
    assert TEACH_MAX_SECONDS == 600
    user = SimpleNamespace(id="u1")
    row = start_teaching(user, None)
    assert row["max_seconds"] == 600
    # Narration is opt-in from the browser; nothing is recorded from a microphone by default.
    assert row["records_audio"] is False
    assert row["audio_optional"] is True
    assert row["teaching"] is True
