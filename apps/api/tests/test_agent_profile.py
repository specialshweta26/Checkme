"""Every agent knows who it is and which skills it has; tasks route to the skill that fits."""

from __future__ import annotations

from types import SimpleNamespace

from app.skills import profile

AGENT = SimpleNamespace(name="tony", title="web Researcher", focus="research", instructions="Find things on the web.")
SKILLS = [
    {"id": "s1", "name": "Yc skill", "kind": "taught", "status": "ready", "instruction": "Open ycombinator.com/companies and filter startups by batch. 1. Open the site."},
    {"id": "s2", "name": "Rent a scooter in Goa", "kind": "taught", "status": "ready", "instruction": "Find scooter rentals in Anjuna with phone numbers and daily prices."},
    {"id": "s3", "name": "Taught workflow", "kind": "taught", "status": "recording", "instruction": ""},
    {"id": "s4", "name": "Weekly report", "kind": "saved", "status": "writing", "instruction": ""},
]


def fake_embed(table):
    async def embed(texts):
        return [table(t) for t in texts]

    return embed


def test_index_lists_every_usable_skill_with_one_line_use():
    block = profile.capabilities_block(AGENT, SKILLS, [], "What skill you have")
    assert "you are tony, web Researcher (focus: research)" in block
    assert "Yc skill [taught, id s1]: Open ycombinator.com/companies and filter startups by batch." in block
    assert "Rent a scooter in Goa" in block
    assert "Weekly report [saved, write-up in progress" in block
    assert "id s3" not in block  # still recording
    assert "answer it from this profile" in block and "Do not run a skill" in block


def test_no_skills_is_said_plainly():
    assert "no saved or taught skills" in profile.capabilities_block(AGENT, [], [], "what can you do")


async def test_what_skills_question_never_runs_a_skill():
    # The bug: "What skill you have" matched "Yc skill" on the word "skill" and the agent ran it.
    for q in ("What skill you have", "what skills do you have?", "list your skills", "what can you do", "who are you"):
        assert profile.is_meta_question(q), q
        assert await profile.route_skills(SKILLS, q, embed=fake_embed(lambda t: [1.0, 0.0])) == []


async def test_matching_task_loads_the_skill():
    def vec(text):
        t = text.lower()
        return [1.0, 0.0] if ("scooter" in t or "bike" in t) else [0.0, 1.0]

    picked = await profile.route_skills(SKILLS, "book me a bike for tomorrow in Anjuna", embed=fake_embed(vec))
    assert [s["id"] for s in picked][:1] == ["s2"]
    assert picked[0]["match"]["by"] in {"embeddings", "keywords"}
    block = profile.capabilities_block(AGENT, SKILLS, picked, "book me a bike")
    assert "### Skill: Rent a scooter in Goa" in block and "which skill you are using" in block


async def test_routing_works_without_embeddings():
    async def broken(texts):
        raise RuntimeError("no key")

    picked = await profile.route_skills(SKILLS, "scooter rentals in Anjuna with prices", embed=broken)
    assert picked and picked[0]["id"] == "s2"
    assert await profile.route_skills(SKILLS, "summarize my inbox", embed=broken) == []


def test_system_prompt_carries_profile_every_turn():
    from app.agent.deep import system_prompt

    user = SimpleNamespace(timezone="Asia/Kolkata", display_name="Shweta")
    text = system_prompt(user, [], "", agent=AGENT, skills=SKILLS, matched_skills=[], query="What skill you have")
    assert "Your skills (3)" in text and "Yc skill" in text and "get_skill" in text
