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


def _run_memory_page(*names):
    elements = [{"ref": f"e{i}", "role": "button", "name": n} for i, n in enumerate(names)]
    return {"ok": True, "url": "https://shop.example/items", "title": "Shop", "text": "Products on sale today",
            "tree": "\n".join(f"- e{i} button {n}" for i, n in enumerate(names)), "elements": elements}


def test_with_progress_lists_what_already_happened():
    text = fb.with_progress("Ran 1 action.", {"done": ["click Add", "click Finish"], "notes": ["Total $25.90"]})
    assert "1. click Add" in text and "2. click Finish" in text and "Total $25.90" in text and text.endswith("Ran 1 action.")


def test_once_labels_finds_controls_that_must_not_repeat():
    page = _run_memory_page("Add to cart", "Finish", "Place your order")
    acts = [{"kind": "click", "ref": "e0"}, {"kind": "click", "ref": "e1"}, {"kind": "click", "ref": "e2"}]
    assert fb.once_labels(acts, page) == ["Finish", "Place your order"]


async def test_a_task_never_clicks_finish_twice_and_remembers_its_steps(monkeypatch):
    from types import SimpleNamespace

    seen = []

    async def plan_step(model, goal, state, values, last, allow_final):
        seen.append(last)
        return {"actions": [{"kind": "click", "ref": "e0"}], "done": False, "result": "", "need_user": "", "why": "", "notes": "Total $25.90"}

    async def navigate(ctx, url):
        return _run_memory_page("Finish")

    async def act(ctx, actions):
        return {**_run_memory_page("Finish"), "completed": len(actions)}

    async def snapshot(ctx, find=""):
        return _run_memory_page("Finish")

    async def no_log(*a, **k):
        return None

    monkeypatch.setattr(fb, "plan_step", plan_step)
    monkeypatch.setattr(fb, "_navigate", navigate)
    monkeypatch.setattr(fb, "_act", act)
    monkeypatch.setattr(fb, "_snapshot", snapshot)
    monkeypatch.setattr(fb, "_log", no_log)
    ctx = SimpleNamespace(connectors={}, user=None, used_tools=[])
    clicks = []
    real_act = fb._act

    async def counting(ctx, actions):
        clicks.append(actions)
        return await real_act(ctx, actions)

    monkeypatch.setattr(fb, "_act", counting)
    out = await fb._browser_task(ctx, "finish the order", "https://shop.example/items", allow_final_action=True, use_saved=False, save=False)
    assert len(clicks) == 1
    assert out["stopped_repeat"] == "Finish" and out["notes"] == ["Total $25.90"]
    assert out["done_so_far"] and "do_not_repeat" in out
    assert "Done so far" in seen[1]
