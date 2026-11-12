"""A skill as a flow of nodes: built from its numbered steps, run strictly in order, a passed step never repeats."""

from types import SimpleNamespace

from app.skills import flow
from app.skills import task_state as ts

INLINE = (
    "1. Open the reporting site at https://example.test/report and sign in if prompted. 2. Open Report Explorer. "
    "3. Pick the report and set Order created from 05/10/2026 to 13/10/2026. Done when: both dates show in the form. "
    "4. Click Generate Report and wait for the success message. 5. Go to Request Status and click Refresh till the "
    "download arrow appears. 6. Download the report. Done when: the file is in Downloads."
)


def test_inline_numbered_steps_become_a_chain_and_dates_are_not_steps():
    f = flow.build({"instruction": INLINE})
    assert [n["n"] for n in f["nodes"]] == [1, 2, 3, 4, 5, 6]
    assert f["nodes"][2]["done_when"] == "both dates show in the form" and "05/10/2026" in f["nodes"][2]["do"]
    assert f["nodes"][4]["kind"] == "wait" and f["nodes"][0]["kind"] == "step"
    assert f["nodes"][0]["next"] == ["n2"] and f["nodes"][-1]["next"] == []


def test_a_skill_without_numbers_is_one_node():
    f = flow.build({"instruction": "Open the sheet and add the rows."})
    assert len(f["nodes"]) == 1 and f["start"] == "n1"


def _run():
    return flow.fresh_run(flow.build({"instruction": "1. Open the cart. Done when: the cart shows. 2. Delete every item. 3. Open Place Order."}))


def test_steps_pass_in_order_with_evidence_seen_on_the_page():
    run = _run()
    early = flow.advance(run, 2, "cart is empty", ["Cart is empty"])
    assert not early["ok"] and "current step is 1" in early["message"]
    unseen = flow.advance(run, 1, "Products in cart", ["STORE Home"])
    assert not unseen["ok"] and "wasn't in anything" in unseen["message"] and "the cart shows" in unseen["message"]
    ok = flow.advance(run, 1, "“Products”", ["Products Pic Title Price"])
    assert ok["ok"] and ok["run"]["current"] == "n2" and "Next is step 2" in ok["message"]


def test_a_passed_step_never_runs_again():
    run = flow.advance(_run(), 1, "Products", ["Products"])["run"]
    again = flow.advance(run, 1, "Products", ["Products"])
    assert not again["ok"] and again["repeat"] and "never runs again" in again["message"] and "step 2" in again["message"]


def test_evidence_is_required_and_must_be_real_words():
    run = _run()
    assert not flow.advance(run, 1, "", ["Products"])["ok"]
    assert not flow.advance(run, 1, "a", ["a cart"])["ok"]


def test_without_the_computer_a_step_passes_unchecked():
    out = flow.advance(_run(), 1, "Opened it", [], checked=False)
    assert out["ok"] and out["run"]["nodes"][0]["checked"] is False


def test_the_prompt_shows_passed_steps_and_only_the_current_one_to_do():
    run = flow.advance(_run(), 1, "Products", ["Products"])["run"]
    text = "\n".join(flow.prompt_lines(run))
    assert "✓ 1." in text and "Don't redo it" in text and "→ 2. NOW: Delete every item" in text and "skill_progress(step=2" in text


async def test_a_task_state_runs_the_flow_and_refuses_repeats(task_store):
    user = SimpleNamespace(id=__import__("uuid").uuid4())
    skill = {"id": "s1", "name": "Cart", "instruction": "1. Open the cart. 2. Delete every item. 3. Open Place Order."}
    state, _ = await ts.begin(user, ts.task_key_for(job_id="j9"), skill=skill)
    assert state["step_count"] == 3 and state["flow"]["current"] == "n1"
    first = await ts.pass_step(state["id"], 1, "Products", ["Products in the cart"])
    assert first["ok"] and first["state"]["step_index"] == 1 and first["state"]["flow"]["current"] == "n2"
    repeat = await ts.pass_step(state["id"], 1, "Products", ["Products in the cart"])
    assert not repeat["ok"] and repeat["repeat"]
    # After a restart, the same task resumes at step 2, and the prompt says so.
    again, resumed = await ts.begin(user, ts.task_key_for(job_id="j9"), skill=None)
    assert resumed and again["flow"]["current"] == "n2" and "→ 2. NOW" in ts.prompt_block(again, resumed=True)
    assert ts.serialize(again)["flow"]["nodes"][0]["status"] == "passed"
