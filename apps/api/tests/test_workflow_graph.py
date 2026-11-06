"""Saved runs as a graph with variables (docs/BROWSING.md "Variables" and "Checks")."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.agent import fast_browser as fb

FORM = [
    {"kind": "fill", "target": {"role": "combobox", "label": "From", "name_attr": "fromCity"}, "text": "Mumbai"},
    {"kind": "fill", "target": {"role": "combobox", "label": "To", "name_attr": "toCity"}, "text": "Deoghar"},
    {"kind": "fill", "target": {"role": "textbox", "label": "Departure date", "input_type": "date"}, "text": "2026-11-21"},
    {"kind": "fill", "target": {"input_type": "password", "name_attr": "pwd"}, "param": "password", "secret": True},
    {"kind": "click", "target": {"role": "button", "name": "Search"}},
]  # fmt: skip


# --- variables ----------------------------------------------------------------------------------------------------
def test_typed_values_become_named_variables_with_label_and_example():
    steps = fb.make_variables(FORM)
    assert [s.get("param") for s in steps[:3]] == ["from", "to", "departure_date"]
    assert steps[0]["example"] == "Mumbai" and steps[0]["label"] == "From" and "text" not in steps[0]
    assert steps[3]["param"] == "password" and "example" not in steps[3]  # secrets keep only a name
    assert steps[4] == FORM[4]
    params = fb.trace_params(steps)
    assert params[0] == {"name": "from", "secret": False, "label": "From", "example": "Mumbai"}
    assert params[3] == {"name": "password", "secret": True}


def test_a_variable_not_given_uses_its_recorded_value_but_a_secret_must_be_given():
    steps = fb.make_variables(FORM)
    out, missing = fb.replay_steps(steps, {"to": "Patna"})
    assert missing == ["password"]
    out, missing = fb.replay_steps(steps, {"to": "Patna", "password": "x"})
    assert missing == [] and [s.get("text") for s in out[:3]] == ["Mumbai", "Patna", "2026-11-21"]
    assert all("example" not in s and "label" not in s for s in out)


async def test_variables_are_filled_from_the_users_message(monkeypatch):
    seen = {}

    async def chat_complete(messages, **kw):
        seen["user"] = messages[1]["content"]
        seen["model"] = kw.get("model")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(
            {"values": {"from": "Mumbai", "to": "Deoghar", "departure_date": "2026-11-21", "password": "nope", "other": "x"}})))])

    import app.llm

    monkeypatch.setattr(app.llm, "chat_complete", chat_complete)
    params = fb.trace_params(fb.make_variables(FORM))
    got = await fb.fill_variables(params, "flights bombay to deoghar on 21st november 2026", {"to": "Given"})
    # Only missing, non-secret fields are asked for and kept; the caller's own values are never replaced.
    assert got == {"from": "Mumbai", "departure_date": "2026-11-21"}
    asked = json.loads(seen["user"].split("Fields:\n", 1)[1].split("\n\nReply", 1)[0])
    assert [f["name"] for f in asked] == ["from", "departure_date"] and asked[0]["example"] == "Mumbai"
    assert "password" not in seen["user"] and seen["model"] == fb.fast_model()
    assert await fb.fill_variables(params, "", {}) == {}


async def test_replay_named_fills_variables_from_the_chat_message(monkeypatch):
    trace = {"id": "t1", "name": "flight search", "host": "travel.test", "start_url": "https://travel.test/",
             "steps": fb.make_variables(FORM[:3] + FORM[4:]), "params": fb.trace_params(fb.make_variables(FORM[:3] + FORM[4:]))}  # fmt: skip
    sent = []

    class Store:
        async def candidates(self, *a, **k):
            return [trace]

        async def mark(self, *a):
            pass

        async def propose(self, *a):
            return False

    async def internal(c, name, args):
        sent.append([s.get("text") for s in args["steps"]])
        return {"ok": True, "completed": len(args["steps"])}

    async def nav(c, url):
        return {"ok": True, "url": url}

    async def fill(params, text, given):
        assert "Patna" in text
        return {"to": "Patna"}

    monkeypatch.setattr(fb, "STORE", Store())
    monkeypatch.setattr(fb, "_internal", internal)
    monkeypatch.setattr(fb, "_navigate", nav)
    monkeypatch.setattr(fb, "fill_variables", fill)
    ctx = SimpleNamespace(user=SimpleNamespace(id="u1"), query="search flights from Mumbai to Patna")
    out = await fb.replay_named(ctx, run_id="t1")
    assert out["ok"] and out["variables_filled"] == {"to": "Patna"}
    assert sent[0][:3] == ["Mumbai", "Patna", "2026-11-21"]


# --- checks (branches) --------------------------------------------------------------------------------------------
def test_check_steps_are_built_validated_and_described():
    built = fb.build_steps(FORM, [{"keep": 4}, {"check": "Does the page say no flights found?", "yes": 0, "no": 3}, {"keep": 0}])
    assert built[1] == {"kind": "check", "text": "Does the page say no flights found?", "yes": 0, "no": 3}
    assert fb.describe_step(built[1]) == "Check: Does the page say no flights found? (yes → the end, no → step 3)"
    for bad, msg in (([{"check": ""}], "write the yes/no"), ([{"check": "q", "yes": 9}], "isn't another step"),
                     ([{"check": "q", "yes": 1}], "isn't another step"), ([{"check": "q", "yes": 0, "no": "x"}], "step number")):  # fmt: skip
        with pytest.raises(ValueError, match=msg):
            fb.build_steps(FORM, bad)


async def test_replay_branches_on_a_check_and_stops_a_loop(monkeypatch):
    ran = []

    async def internal(c, name, args):
        ran.extend(s["target"]["name"] for s in args["steps"])
        return {"ok": True, "completed": len(args["steps"]), "url": "https://t/", "tree": "x"}

    answers = []

    async def check(ctx, question, state):
        return answers.pop(0)

    monkeypatch.setattr(fb, "_internal", internal)
    monkeypatch.setattr(fb, "check_page", check)

    def click(name):
        return {"kind": "click", "target": {"role": "button", "name": name}}

    steps = [click("Search"), {"kind": "check", "text": "No results?", "yes": 4, "no": 3}, click("First result"), click("Try nearby dates")]
    trace = {"id": "t", "start_url": "", "steps": steps}
    answers[:] = [False]
    assert (await fb.replay(SimpleNamespace(), trace, {}))["ok"] and ran == ["Search", "First result", "Try nearby dates"]
    ran.clear()
    answers[:] = [True]
    assert (await fb.replay(SimpleNamespace(), trace, {}))["ok"] and ran == ["Search", "Try nearby dates"]
    ran.clear()
    loop = [click("Next page"), {"kind": "check", "text": "More pages?", "yes": 1, "no": 0}]
    answers[:] = [True] * 10
    out = await fb.replay(SimpleNamespace(), {"id": "l", "start_url": "", "steps": loop}, {})
    assert out["ok"] is False and "loop" in out["error"] and ran == ["Next page"] * fb.MAX_VISITS


async def test_check_page_answers_no_when_unclear(monkeypatch):
    async def broken(*a, **k):
        raise RuntimeError("model down")

    import app.llm

    monkeypatch.setattr(app.llm, "chat_complete", broken)
    assert await fb.check_page(SimpleNamespace(), "Any flights?", {"tree": "- e1 link Flight 6E 123"}) is False

    async def yes(*a, **k):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"answer": "yes"}'))])

    monkeypatch.setattr(app.llm, "chat_complete", yes)
    assert await fb.check_page(SimpleNamespace(), "Any flights?", {"tree": "- e1 link Flight 6E 123"}) is True


def test_saved_run_rows_name_their_variables_and_checks():
    row = SimpleNamespace(
        id="t", name="n", goal="g", host="h", start_url="", source="task", skill_id="", params=[], uses=0, successes=0, failures=0,
        steps=[fb.make_variables(FORM)[0], {"kind": "check", "text": "No results?", "yes": 0, "no": 2}],
        proposed_steps=None, proposed_note=None, proposed_at=None, prior_steps=None, run_key_hash="", updated_at=None,
    )  # fmt: skip
    rows = fb.describe_trace(row)["step_rows"]
    assert rows[0]["variable"] == "from" and rows[0]["label"] == "From"
    assert rows[1]["check"] == {"text": "No results?", "yes": 0, "no": 2}
