from types import SimpleNamespace

from app.agents.collab import (
    GROUP_MEMBER_MAX,
    GROUP_MEMBER_MIN,
    MAX_CHALLENGES,
    MAX_POSTS,
    TEACH_MAX_SECONDS,
    apply_post,
    creator_name,
    due_turn,
    is_group_hello,
    is_kickoff,
    is_roll_call,
    new_board,
    parse_mentions,
    pick_owner,
    role_line,
    routing_prompt,
    run_shared_task,
    too_similar,
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
    assert "work_together" in lowered
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
    assert "work_together" not in lowered
    assert "one8" not in lowered


def test_shared_task_stops_on_repeat_and_challenge_cap():
    a = _bot("a1", "Analyst")
    r = _bot("r1", "Researcher")
    board = new_board("Ship the weekly pack", [a, r])
    assert apply_post(board, "a1", "propose", "Analyst will count the rows in the weekly pack.")["ok"]
    assert apply_post(board, "r1", "propose", "Researcher will read the source notes for the pack.")["ok"]
    turn = due_turn(board)
    assert turn["kinds"] == ["respond", "challenge", "converge"]
    challenged = apply_post(board, turn["agent_id"], "challenge", "The row count is not enough without the source notes.", "r1" if turn["agent_id"] == "a1" else "a1")
    assert challenged["ok"]
    reply = due_turn(board)
    assert reply["kinds"] == ["respond", "converge"]
    assert apply_post(board, reply["agent_id"], "respond", "The notes change two totals, so the count has to wait.")["ok"]
    again = apply_post(board, due_turn(board)["agent_id"], "respond", "The notes change two totals, so the count has to wait.")
    assert again["ok"] is False
    assert too_similar("The notes change two totals, so the count has to wait.", "The notes change two totals, so the count has to wait.")
    assert MAX_CHALLENGES == 2


async def test_shared_task_converges_and_caps_posts():
    a = SimpleNamespace(id="a1", name="Analyst", title="tables", instructions="Counts.", focus="research")
    r = SimpleNamespace(id="r1", name="Researcher", title="sources", instructions="Reads notes.", focus="research")

    async def scripted(agent, board, kinds):
        if "propose" in kinds:
            claim = "count every row in the sheet" if agent.name == "Analyst" else "read the source notes only"
            return {"kind": "propose", "text": f"{agent.name} will {claim}."}
        if len(board["posts"]) >= 3:
            return {"kind": "converge", "text": "Outcome: Analyst counts rows and Researcher reads notes, then stop."}
        return {"kind": "respond", "text": f"{agent.name} checked a fresh detail: {agent.title} changes the pack."}

    board = await run_shared_task([a, r], "Ship the weekly pack", scripted)
    assert board["reason"] == "converged"
    assert "stop" in board["outcome"]
    kinds = [p["kind"] for p in board["posts"]]
    assert kinds[0] == "propose" and kinds[1] == "propose"
    assert kinds[-1] == "converge"
    assert len(board["posts"]) <= MAX_POSTS

    async def never_stop(agent, board, kinds):
        n = len(board["posts"])
        kind = "propose" if "propose" in kinds else "respond"
        lines = [
            "Count only the paid orders in March.",
            "Open the supplier folder and read the notes.",
            "The tax column skips two weeks of data.",
            "Refunds cluster on the west region.",
            "Inventory names do not match the sheet.",
            "Shipping fees were entered twice.",
            "Margin uses last month's prices.",
            "Payroll is outside this pack.",
            "Leave the draft charts untouched.",
        ]
        return {"kind": kind, "text": lines[n]}

    capped = await run_shared_task([a, r], "Keep going", never_stop)
    assert capped["reason"] == "limit"
    assert len(capped["posts"]) == MAX_POSTS
    assert capped["outcome"] == ""  # stopping at the limit is not agreement


async def test_work_together_posts_each_voice_once(monkeypatch):
    from app.agent.deep import RunContext, _work_together

    async def fake(members, task, speak=None, on_post=None):
        return {
            "reason": "converged",
            "outcome": "Analyst counts and Researcher reads.",
            "posts": [
                {"agent_id": "a1", "name": "Analyst", "kind": "propose", "text": "I will count the rows."},
                {"agent_id": "r1", "name": "Researcher", "kind": "converge", "text": "Analyst counts and Researcher reads."},
            ],
        }

    monkeypatch.setattr("app.agents.collab.run_shared_task", fake)
    owner = _bot("a1", "Analyst")
    peer = _bot("r1", "Researcher")
    ctx = RunContext(
        db=None,
        user=SimpleNamespace(id="u"),
        connectors={},
        query="Ship the pack",
        agent=owner,
        group=SimpleNamespace(name="Insights"),
        teammates=[owner, peer],
    )
    out = await _work_together(ctx, "")
    assert out["reason"] == "converged"
    assert [p["agent_name"] for p in ctx.thread] == ["Analyst", "Researcher"]
    assert ctx.thread[0]["peer"] is True
    assert "work_together" in ctx.used_tools
    again = await _work_together(ctx, "again")
    assert "already ran" in again["error"]


def _pair():
    a = SimpleNamespace(id="a1", name="Analyst", title="tables", instructions="Counts.", focus="research")
    r = SimpleNamespace(id="r1", name="Researcher", title="sources", instructions="Reads notes.", focus="research")
    return a, r


async def test_a_failed_move_ends_the_discussion_without_invented_posts():
    async def fails_second(agent, board, kinds):
        if board["posts"]:
            return None
        return {"kind": "propose", "text": "Analyst will count every row."}

    board = await run_shared_task(list(_pair()), "Ship it", fails_second)
    assert board["reason"] == "failed" and board["outcome"] == ""
    assert [p["text"] for p in board["posts"]] == ["Analyst will count every row."]


async def test_a_disallowed_move_is_not_replaced_with_something_else():
    async def wrong(agent, board, kinds):
        return {"kind": "converge", "text": "We all agree on everything here."}

    board = await run_shared_task(list(_pair()), "Ship it", wrong)
    assert board["reason"] == "failed" and board["posts"] == []


def test_a_forced_move_keeps_the_models_words():
    from app.agents.collab import _parse_move

    raw = '{"kind": "converge", "text": "I will read the source notes first."}'
    assert _parse_move(raw, ["propose"])["kind"] == "propose"
    assert _parse_move(raw, ["propose"])["text"] == "I will read the source notes first."
    assert _parse_move('{"kind": "challenge", "text": "That is wrong for this task."}', ["respond", "converge"]) is None


def test_real_model_reply_shapes_are_read():
    from app.agents.collab import _parse_move

    one = '{"kind": "respond", "text": "Check the refund dates first.", "to": ""}'
    assert _parse_move(one + one, ["respond", "converge"])["text"] == "Check the refund dates first."
    assert _parse_move(one + "\n }\n }", ["respond", "converge"])["kind"] == "respond"
    late = '{"kind": "propose", "text": "I will build the timeline next."}'
    assert _parse_move(late, ["respond", "challenge", "converge"])["kind"] == "respond"
    assert _parse_move('{"kind": "propo', ["propose"]) is None  # cut-off output is still not used


async def test_with_no_api_key_nothing_is_posted(monkeypatch):
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(openai_api_key=""))
    board = await run_shared_task(list(_pair()), "Ship it")
    assert board["reason"] == "failed" and board["posts"] == []


async def test_a_model_failure_is_retried_then_reported(monkeypatch):
    from app.agents import collab

    calls = []

    async def chat(messages, **kw):
        calls.append(kw)
        if len(calls) == 1:
            raise RuntimeError("model down")
        content = '{"kind": "propose", "text": "Analyst will count every row."}' if len(calls) == 2 else '{"kind": "propo'
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(openai_api_key="k", openai_model="m", openai_small_model="small"))
    monkeypatch.setattr("app.llm.chat_complete", chat)
    monkeypatch.setattr("app.llm.get_settings", lambda: SimpleNamespace(openai_model="m", openai_small_model="small"))
    agent, peer = _pair()
    board = collab.new_board("Ship it", [agent, peer])
    move = await collab.model_move(agent, board, ["propose"])
    assert move["kind"] == "propose" and calls[1]["model"] == "small"
    assert calls[1]["max_tokens"] == collab.MOVE_MAX_TOKENS
    assert await collab.model_move(agent, board, ["propose"]) is None  # cut-off output is not used, even on retry
    assert len(calls) == 4


async def test_each_post_is_reported_as_it_happens():
    seen = []

    async def scripted(agent, board, kinds):
        if "propose" in kinds:
            claim = "count every row in the sheet" if agent.name == "Analyst" else "read the source notes only"
            return {"kind": "propose", "text": f"{agent.name} will {claim}."}
        return {"kind": "converge", "text": "Agreed: each of us keeps the piece we claimed."}

    async def on_post(post):
        seen.append(post["kind"])

    board = await run_shared_task(list(_pair()), "Ship it", scripted, on_post)
    assert seen == ["propose", "propose", "converge"] and board["reason"] == "converged"
    assert board["outcome"] == "Agreed: each of us keeps the piece we claimed."


async def test_work_together_shows_progress_colours_and_failure(monkeypatch):
    from app.agent.deep import RunContext, _work_together

    async def fake(members, task, speak=None, on_post=None):
        post = {"agent_id": "a1", "name": "Analyst", "kind": "propose", "text": "I will count the rows."}
        await on_post(post)
        return {"reason": "failed", "outcome": "", "posts": [post]}

    emitted = []

    class Progress:
        def __init__(self, name=""):
            self.name = name

        def child(self, name):
            return Progress(name)

        async def emit(self, text, kind="step", tool=""):
            emitted.append((self.name, text))

    monkeypatch.setattr("app.agents.collab.run_shared_task", fake)
    owner = SimpleNamespace(id="a1", name="Analyst", color="#123", avatar="a.png")
    peer = SimpleNamespace(id="r1", name="Researcher", color="", avatar="")
    ctx = RunContext(db=None, user=SimpleNamespace(id="u"), connectors={}, query="Ship", agent=owner, group=SimpleNamespace(name="G"), teammates=[owner, peer], progress=Progress())
    out = await _work_together(ctx, "")
    assert ctx.thread[0]["agent_color"] == "#123" and ctx.thread[0]["agent_avatar"] == "a.png"
    assert emitted == [("Analyst", "I will count the rows.")]
    assert "did not reach agreement" in out["note"] and out["outcome"] == ""


async def test_teaching_is_ten_minutes_with_optional_narration(skill_store, monkeypatch):
    from app.skills import recorder

    monkeypatch.setattr(recorder, "start", lambda *a: None)
    assert TEACH_MAX_SECONDS == 600
    user = SimpleNamespace(id="00000000-0000-0000-0000-0000000000b1")
    row = await start_teaching(user, None)
    assert row["max_seconds"] == 600
    # Narration is opt-in from the browser; nothing is recorded from a microphone by default.
    assert row["records_audio"] is False
    assert row["audio_optional"] is True
    assert row["teaching"] is True
