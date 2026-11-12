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
    assert not unseen["ok"] and "isn't on the screen now" in unseen["message"] and "the cart shows" in unseen["message"]
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


def test_the_skill_progress_tool_accepts_evidence():
    from app.agent.deep import SkillProgressInput

    assert "evidence" in SkillProgressInput.model_fields
    assert SkillProgressInput(step=1, evidence="Products").evidence == "Products"


def test_a_removal_passes_with_gone_once_the_words_left_the_page():
    run = flow.advance(_run(), 1, "Products", ["Products"])["run"]
    before, after = "Products MacBook Pro 1100 Delete", "Products Total"
    assert flow.advance(run, 2, "gone: MacBook Pro", [before, after])["ok"]
    assert not flow.advance(run, 2, "gone: MacBook Pro", [before, before])["ok"]  # still there
    assert not flow.advance(run, 2, "gone: Sony vaio", [before, after])["ok"]  # never there
    assert not flow.advance(run, 2, "gone: MacBook Pro", [after])["ok"]  # nothing earlier to compare with


def test_evidence_must_be_on_screen_after_the_step_not_from_before_it():
    run = flow.advance(_run(), 1, "Products", ["Products"])["run"]
    reads = ["Products MacBook Pro 1100 Delete", "Products Total", "Products Total", "Products Total"]
    assert not flow.advance(run, 2, "MacBook Pro 1100 Delete", reads)["ok"]  # only before the delete
    assert flow.advance(run, 2, "gone: MacBook Pro", reads)["ok"]



# ---------------------------------------------------------------- editing


def _edited():
    return {
        "nodes": [
            {"id": "a", "title": "Open the report list", "do": "Open Report Explorer.", "done_when": "Report Explorer shows"},
            {"id": "b", "title": "Generate", "do": "Pick the report and click Generate Report.", "branches": [{"to": "d", "when": "a report for these dates is already listed"}]},
            {"id": "c", "title": "Wait", "do": "Refresh Request Status until the download arrow appears.", "kind": "wait"},
            {"id": "d", "title": "Download", "do": "Download the report.", "done_when": "the file is in Downloads"},
        ]
    }


def test_an_edited_flow_is_numbered_and_its_branch_kept():
    clean, errors = flow.normalize(_edited())
    assert not errors and [x["id"] for x in clean["nodes"]] == ["n1", "n2", "n3", "n4"]
    assert clean["nodes"][1]["branches"] == [{"to": "n4", "when": "a report for these dates is already listed"}]
    assert clean["nodes"][2]["kind"] == "wait" and clean["nodes"][3]["next"] == []


def test_edits_that_cant_run_are_refused():
    back = _edited()
    back["nodes"][2]["branches"] = [{"to": "a", "when": "it failed"}]
    assert any("only go forward" in e for e in flow.normalize(back)[1])
    empty = _edited()
    empty["nodes"][0]["do"] = empty["nodes"][0]["title"] = ""
    assert any("says nothing to do" in e for e in flow.normalize(empty)[1])
    assert flow.normalize({"nodes": []})[1]


def test_the_text_is_written_back_from_the_flow():
    clean, _ = flow.normalize(_edited())
    text = flow.to_instruction(clean, "Report for the dates given.")
    assert text.startswith("Report for the dates given.\n1. Open Report Explorer. Done when: Report Explorer shows.")
    assert "If a report for these dates is already listed, go to step 4." in text
    assert flow.preamble_of("Intro line. 1. Do x. 2. Do y.") == "Intro line."


def test_an_edited_flow_wins_over_the_text():
    clean, _ = flow.normalize(_edited())
    built = flow.build({"instruction": "1. Something else. 2. Other.", "flow": clean})
    assert len(built["nodes"]) == 4 and built["nodes"][1]["branches"]


def test_a_branch_jumps_forward_and_skips_what_it_passes_over():
    run = flow.fresh_run(flow.normalize(_edited())[0])
    run = flow.advance(run, 1, "Report Explorer", ["Report Explorer"])["run"]
    wrong = flow.advance(run, 2, "already listed", ["already listed"], go_to=3)
    assert wrong["ok"]  # step 3 is the usual next step, so it is allowed
    out = flow.advance(run, 2, "already listed", ["already listed"], go_to=4)
    assert out["ok"] and out["run"]["current"] == "n4" and flow.node(out["run"], "n3")["status"] == "skipped"
    bad = flow.advance(run, 2, "already listed", ["already listed"], go_to=1)
    assert not bad["ok"] and "can't go to step 1" in bad["message"]
    text = "\n".join(flow.prompt_lines(run))
    assert "If a report for these dates is already listed: go to step 4" in text and "next_step=4" in text


def test_a_skill_row_returns_its_edited_flow():
    from types import SimpleNamespace as NS

    from app.skills import service

    clean, _ = flow.normalize(_edited())
    row = NS(instruction="1. a. 2. b.", name="R", flow=clean)
    assert len(service._flow_of(row)["nodes"]) == 4
    row.flow = {}
    assert len(service._flow_of(row)["nodes"]) == 2


def test_written_back_steps_end_their_sentences():
    clean, _ = flow.normalize({"nodes": [{"id": "a", "do": "Open the cart", "done_when": "Products shows", "branches": [{"to": "b", "when": "it is empty"}]}, {"id": "b", "do": "Report"}]})
    assert flow.to_instruction(clean).split("\n")[0] == "1. Open the cart. If it is empty, go to step 2. Done when: Products shows."

