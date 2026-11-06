"""Long browser work (docs/BROWSING.md "Long tasks"): the fast loop sizes itself to the goal, long skills move to a
background job, and background jobs get a bigger step budget."""

from app.agent import deep
from app.agent import fast_browser as fb
from app.jobs import runner
from app.skills import task_state as ts

LONG_GOAL = """Download the order status report:
1. Open WMS
2. Hover Reporting and open Report Explorer
3. Search for Order Status
4. Set From and To to last week
5. Apply, then export CSV"""


def test_short_goals_keep_the_fast_default():
    assert not fb.is_long_goal("Log in to Acme and open my statements")
    assert fb.round_budget("Log in to Acme") == (12, False)


def test_long_goals_get_the_main_model_and_more_rounds():
    assert fb.is_long_goal(LONG_GOAL)
    assert fb.is_long_goal("open a then b then c then d then e then f")
    assert fb.is_long_goal(" ".join(["word"] * 130))
    assert fb.round_budget(LONG_GOAL) == (fb.LONG_ROUNDS, True)


def test_callers_max_steps_wins_but_is_capped():
    assert fb.round_budget("x", 5) == (5, False)
    assert fb.round_budget(LONG_GOAL, 100) == (fb.MAX_ROUNDS, True)


async def test_long_goal_starts_on_the_main_model(monkeypatch):
    from types import SimpleNamespace

    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "browser_fast_model", "fast-x")
    monkeypatch.setattr(get_settings(), "browser_escalate_model", "strong-y")
    models = []

    async def plan_step(model, goal, state, values, last, allow_final):
        models.append(model)
        return {"actions": [], "done": True, "result": "done", "need_user": ""}

    async def snap(c, find=""):
        return {"url": "https://wms.test/", "title": "", "text": "", "tree": ""}

    monkeypatch.setattr(fb, "plan_step", plan_step)
    monkeypatch.setattr(fb, "_snapshot", snap)
    await fb.browser_task(SimpleNamespace(user=None), LONG_GOAL, use_saved=False, save=False)
    await fb.browser_task(SimpleNamespace(user=None), "open the home page", use_saved=False, save=False)
    assert models == ["strong-y", "fast-x"]


def _state(steps, done=0):
    return {"skill_name": "Order Status Report", "skill_id": "s1", "step_count": steps, "step_index": done}


def test_long_skill_in_chat_moves_to_the_background():
    block = ts.prompt_block(_state(40), can_background=True)
    assert "start_background_task" in block and "40 steps" in block


def test_short_skill_or_background_or_resumed_task_stays():
    assert "start_background_task" not in ts.prompt_block(_state(6), can_background=True)
    assert "start_background_task" not in ts.prompt_block(_state(40), can_background=False)
    assert "start_background_task" not in ts.prompt_block(_state(40, done=3), can_background=True)


def test_background_jobs_get_a_bigger_budget_and_phase_checkpoints():
    assert deep.BACKGROUND_STEP_LIMIT > 200 and deep.BACKGROUND_STEP_CAP >= deep.BACKGROUND_STEP_LIMIT
    brief = runner.ROLE_BRIEF["worker"]
    assert "one todo per phase" in brief and "skill_progress" in brief
