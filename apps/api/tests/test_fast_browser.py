"""Fast browser paths (docs/BROWSING.md "Fast browser"): stable targets, verified replay, batching with a fast
model, saved traces, direct downloads, the daemon ready cache and the teach DOM recorder. All offline."""

from __future__ import annotations

import json
import logging
import uuid
from types import SimpleNamespace

import pytest

from app.agent import fast_browser as fb
from app.connectors import computer_daemon as d
from app.connectors import daytona_box as box
from app.config import get_settings


# --- daemon: stable targets, verification, waits ------------------------------------------------------------------
class Loc:
    def __init__(self, n=1, value=""):
        self.n, self.value = n, value

    def count(self):
        return self.n

    @property
    def first(self):
        return self

    def input_value(self, timeout=0):
        return self.value


class Page:
    def __init__(self, css=None, labels=None, roles=None, url="https://a.test/login"):
        self.css, self.labels, self.roles = css or {}, labels or {}, roles or {}
        self.frames = [self]
        self.url = url
        self.evaluated: list = []
        self.loads: list = []

    def locator(self, sel):
        return self.css.get(sel, Loc(0))

    def get_by_label(self, text, exact=True):
        return self.labels.get(text, Loc(0))

    def get_by_placeholder(self, text, exact=True):
        return Loc(0)

    def get_by_role(self, role, name="", exact=True):
        return self.roles.get((role, name), Loc(0))

    def get_by_text(self, text, exact=True):
        return Loc(0)

    def evaluate(self, js, arg=None):
        self.evaluated.append(arg)
        return True

    def wait_for_load_state(self, state, timeout=0):
        self.loads.append(state)


def test_descriptor_keeps_stable_fields_only():
    node = {"ref": "e4", "role": "textbox", "name": "Email", "tag": "input", "name_attr": "email", "x": 10, "testid": "", "input_type": "email"}
    assert d.descriptor(node) == {"role": "textbox", "name": "Email", "tag": "input", "name_attr": "email", "input_type": "email"}


def test_locate_target_prefers_a_unique_stable_selector():
    one = Loc(1)
    page = Page(css={'[data-testid="login"], [data-test="login"], [data-qa="login"], [data-cy="login"]': one})
    loc, how = d.locate_target(page, {"testid": "login", "role": "button", "name": "Sign in"})
    assert loc is one and how == "testid"


def test_locate_target_skips_generated_ids_and_falls_back_to_role():
    btn = Loc(1)
    page = Page(css={'[id=":r12:"]': Loc(1)}, roles={("button", "Sign in"): btn})
    loc, how = d.locate_target(page, {"id": ":r12:", "role": "button", "name": "Sign in"})
    assert loc is btn and how == "role"
    assert d.locate_target(Page(), {"role": "button", "name": "Nope"}) == (None, "")


def test_locate_target_marks_ambiguous_matches():
    page = Page(css={'input[name="q"]': Loc(3)})
    loc, how = d.locate_target(page, {"tag": "input", "name_attr": "q"})
    assert loc is not None and how == "name*"


def test_verify_step_checks_typed_values_but_never_compares_secrets():
    d._verify_step(Page(), Loc(value="a@b.c"), {"kind": "fill", "text": "a@b.c"}, {"input_type": "email"})
    with pytest.raises(RuntimeError, match="mismatch"):
        d._verify_step(Page(), Loc(value=""), {"kind": "fill", "text": "a@b.c"}, {"input_type": "email"})
    # A password field only has to be non-empty (the page may mask or transform it).
    d._verify_step(Page(), Loc(value="xxxxxx"), {"kind": "fill", "text": "hunter22"}, {"input_type": "password"})
    with pytest.raises(RuntimeError, match="secret field is still empty"):
        d._verify_step(Page(), Loc(value=""), {"kind": "fill", "text": "hunter22"}, {"input_type": "password"})


def test_secret_targets():
    assert d.is_secret_target({"input_type": "password"})
    assert d.is_secret_target({"autocomplete": "one-time-code"})
    assert d.is_secret_target({"name_attr": "cvv"})
    assert not d.is_secret_target({"name_attr": "email", "label": "Email"})


def test_wait_ready_waits_for_load_when_the_url_changed():
    page = Page(url="https://a.test/home")
    ms = d.wait_ready(page, "click", before_url="https://a.test/login")
    # Load of the new document, quiet, then quiet again once it settled.
    assert page.loads and set(page.loads) == {"domcontentloaded"} and page.evaluated == [[250, 2500], [250, 2500]] and ms >= 0
    page2 = Page()
    d.wait_ready(page2, "fill", before_url=page2.url)
    assert page2.loads == [] and page2.evaluated == [[80, 900]]


def test_short_form_pages_count_as_ready():
    assert d._has_fields({"elements": [{"role": "textbox"}, {"role": "button"}]})
    assert not d._has_fields({"elements": [{"role": "link"}]})


def test_tree_line_shows_input_type_and_files():
    assert d.tree_line({"ref": "e2", "role": "textbox", "name": "Password", "input_type": "password"}) == "- e2 textbox Password [password]"
    assert d.tree_line({"ref": "e3", "role": "link", "name": "Statement", "href": "https://b.test/s.pdf?x=1"}).endswith("[file]")
    assert d.tree_line({"ref": "e4", "role": "button", "name": "Go", "input_type": "submit"}) == "- e4 button Go"


def test_file_urls_and_header_names():
    assert d.looks_like_file_url("https://bank.test/statements/2026-09.pdf")
    assert d.looks_like_file_url("https://x.test/report?export=csv")
    assert not d.looks_like_file_url("https://x.test/statements")
    assert not d.looks_like_file_url("javascript:void(0)")
    assert d.filename_from_headers('attachment; filename="Sept 2026.pdf"') == "Sept 2026.pdf"
    assert d.filename_from_headers("attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf") == "résumé.pdf"
    assert d.filename_from_headers("inline") == ""


def test_recorder_drops_secret_values(monkeypatch):
    monkeypatch.setattr(d, "_recording", True)
    d._rec_events.clear()
    d._on_record(None, {"kind": "fill", "target": {"input_type": "password"}, "text": "hunter22", "secret": True})
    d._on_record(None, {"kind": "fill", "target": {"name_attr": "email"}, "text": "a@b.c"})
    assert "text" not in d._rec_events[0] and d._rec_events[0]["secret"] is True
    assert d._rec_events[1]["text"] == "a@b.c"
    d._rec_events.clear()


# --- daytona: ready cache and inline RPC --------------------------------------------------------------------------
class Proc:
    def __init__(self):
        self.commands: list[str] = []
        self._sbx = SimpleNamespace(sandbox_id="sbx-1")

    async def exec(self, command, cwd=None, env=None, timeout=60):
        self.commands.append(command)
        out = '{"ok": true, "url": "https://x.test/"}' if "curl -sS" in command else ""
        return type("R", (), {"exit_code": 0, "result": out})()


def test_ready_cache_keys_and_ttl(monkeypatch):
    proc = Proc()
    key = box.ready_key(proc, "cfg1")
    assert key[1] == "sbx-1" and key[3] == "cfg1"
    box.forget_daemon_ready()
    assert not box.daemon_known_ready(key)
    box.mark_daemon_ready(key)
    assert box.daemon_known_ready(key)
    assert not box.daemon_known_ready(box.ready_key(proc, "cfg2"))  # new settings -> full check
    monkeypatch.setattr(get_settings(), "browser_ready_ttl_s", 0)
    assert not box.daemon_known_ready(key)
    box.forget_daemon_ready()
    assert not box._READY or all(k[0] != key[0] for k in box._READY)


def test_inline_rpc_command_never_embeds_raw_json():
    cmd = box.inline_rpc_command({"command": "act", "actions": [{"kind": "fill", "text": "pa$$ 'w\"rd"}]}, 30)
    assert cmd and "pa$$" not in cmd and "'w" not in cmd


async def test_browser_rpc_logs_timing_without_values(monkeypatch, caplog):
    async def ready(proc, fs=None):
        return None

    monkeypatch.setattr(box, "_ensure_daemon", ready)
    caplog.set_level(logging.INFO, logger=box.log.name)
    await box._browser_rpc(Proc(), {"command": "act", "actions": [{"kind": "fill", "ref": "e1", "text": "s3cretvalue"}]}, 30, None)
    lines = [r.getMessage() for r in caplog.records if "browser.timing" in r.getMessage()]
    assert lines and "command=act" in lines[0] and "fill" in lines[0]
    assert all("s3cretvalue" not in r.getMessage() for r in caplog.records)


# --- fast_browser: values, traces, plans --------------------------------------------------------------------------
def test_fill_values_substitutes_and_reports_missing():
    steps, missing = fb.fill_values([{"kind": "fill", "ref": "e1", "text": "{{email}}"}, {"kind": "fill", "ref": "e2", "text": "{{ otp }}"}], {"email": "a@b.c"})
    assert steps[0]["text"] == "a@b.c" and steps[1]["text"] == "{{ otp }}" and missing == ["otp"]


def test_scrub_hides_values():
    assert fb.scrub("Welcome a@b.c, pw hunter22", {"email": "a@b.c", "password": "hunter22"}) == "Welcome •••, pw •••"


def test_trace_steps_replace_secrets_and_values_with_params():
    planned = [
        {"kind": "fill", "ref": "e1", "text": "{{email}}"},
        {"kind": "fill", "ref": "e2", "text": "hunter22"},
        {"kind": "click", "ref": "e3"},
        {"kind": "click", "ref": "e9"},
    ]
    trace = [
        {"i": 0, "target": {"name_attr": "email"}, "secret": False},
        {"i": 1, "target": {"input_type": "password", "name_attr": "pass"}, "secret": True},
        {"i": 2, "target": {"role": "button", "name": "Sign in"}},
    ]  # step 3 never ran
    steps = fb.trace_steps(planned, trace, {"password": "hunter22"})
    assert steps == [
        {"kind": "fill", "target": {"name_attr": "email"}, "param": "email"},
        {"kind": "fill", "target": {"input_type": "password", "name_attr": "pass"}, "param": "pass", "secret": True},
        {"kind": "click", "target": {"role": "button", "name": "Sign in"}},
    ]
    assert "hunter22" not in json.dumps(steps)
    assert fb.trace_params(steps) == [{"name": "email", "secret": False}, {"name": "pass", "secret": True}]


def test_replay_steps_fill_params():
    steps = [{"kind": "fill", "target": {"name_attr": "email"}, "param": "email"}, {"kind": "wait_for", "ms": 500}]
    out, missing = fb.replay_steps(steps, {"email": "a@b.c"})
    assert out == [{"kind": "fill", "target": {"name_attr": "email"}, "text": "a@b.c"}, {"kind": "wait_for", "wait_ms": 500}]
    assert fb.replay_steps(steps, {})[1] == ["email"]


def test_pick_trace_by_name_or_similar_goal_and_skips_failing():
    rows = [
        {"id": "1", "name": "log in to acme", "goal": "log in to acme", "steps": [{}], "failures": 9, "successes": 0},
        {"id": "2", "name": "log in to acme portal", "goal": "Log in to the Acme portal", "steps": [{}]},
        {"id": "3", "name": "download statement", "goal": "download the latest statement", "steps": [{}]},
    ]
    assert fb.pick_trace(rows, name="log in to acme portal")["id"] == "2"
    assert fb.pick_trace(rows, goal="log in to acme portal please")["id"] == "2"
    assert fb.pick_trace(rows, goal="log in to acme")["id"] == "2"  # the exact one keeps failing
    assert fb.pick_trace(rows, goal="book a flight") is None


def test_parse_plan_filters_kinds_and_caps_batch():
    raw = "```json\n" + json.dumps({"actions": [{"kind": "click", "ref": "e1", "evil": 1}, {"kind": "eval", "js": "x"}] + [{"kind": "press", "text": "Tab"}] * 20, "done": False}) + "\n```"
    plan = fb.parse_plan(raw)
    assert plan["actions"][0] == {"kind": "click", "ref": "e1"} and len(plan["actions"]) == fb.MAX_BATCH
    assert all(a["kind"] != "eval" for a in plan["actions"])
    assert fb.parse_plan("not json") is None


def test_final_click_guard():
    state = {"elements": [{"ref": "e1", "name": "Place order"}, {"ref": "e2", "name": "Continue"}]}
    assert fb.final_click([{"kind": "click", "ref": "e1"}], state) == "Place order"
    assert fb.final_click([{"kind": "click", "ref": "e2"}], state) == ""


def test_for_model_compacts_only_with_a_tree(monkeypatch):
    raw = {"ok": True, "tree": "- e1 button Go", "elements": [{"ref": "e1"}], "screenshot_path": "/tmp/p.png", "trace": [1], "timing": {"total_ms": 42}}
    out = fb.for_model(raw)
    assert out == {"ok": True, "tree": "- e1 button Go", "took_ms": 42}
    assert "elements" in fb.for_model({"ok": True, "elements": [1]})
    monkeypatch.setattr(get_settings(), "browser_compact_snapshot", False)
    assert "elements" in fb.for_model(raw)


def test_dom_events_to_steps_keeps_last_edit_and_masks_secrets():
    u = "https://acme.test/login"
    events = [
        {"kind": "fill", "url": u, "target": {"name_attr": "email"}, "text": "a@"},
        {"kind": "fill", "url": u, "target": {"name_attr": "email"}, "text": "a@b.c"},
        {"kind": "fill", "url": u, "target": {"input_type": "password"}, "secret": True, "text": ""},
        {"kind": "click", "url": u, "target": {"role": "button", "name": "Sign in"}},
        {"kind": "click", "url": "https://acme.test/home", "target": {"role": "link", "name": "Statements"}},  # caused by the click
        {"kind": "fill", "url": "https://acme.test/home", "target": {"name_attr": "q"}, "text": "sept"},
        {"kind": "click", "url": "https://other.test/x", "target": {"role": "button", "name": "Go"}},  # typed address
    ]
    start, steps = fb.dom_events_to_steps(events)
    assert start == u
    fills = [s for s in steps if s["kind"] == "fill"]
    assert len(fills) == 3 and fills[0].get("text") == "a@b.c"
    assert fills[1].get("param") and fills[1].get("secret") and "text" not in fills[1]
    assert [s["kind"] for s in steps] == ["fill", "fill", "click", "click", "fill", "navigate", "click"]
    assert steps[5]["url"] == "https://other.test/x"


# --- fast_browser: the loop ---------------------------------------------------------------------------------------
class FakeStore:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.saved: list[dict] = []
        self.marks: list = []

    async def save(self, user_id, **kw):
        self.saved.append(kw)
        return "t-new"

    async def candidates(self, user_id, host="", trace_id="", skill_id=""):
        return [r for r in self.rows if (not host or r["host"] == host) and (not skill_id or r.get("skill_id") == skill_id)]

    async def mark(self, trace_id, ok):
        self.marks.append((trace_id, ok))


LOGIN = {
    "ok": True, "url": "https://acme.test/login", "title": "Sign in", "text": "Sign in to Acme",
    "tree": "- e1 textbox Email [email]\n- e2 textbox Password [password]\n- e3 button Sign in",
    "elements": [{"ref": "e1", "name": "Email"}, {"ref": "e2", "name": "Password"}, {"ref": "e3", "name": "Sign in"}],
}  # fmt: skip
HOME = {"ok": True, "url": "https://acme.test/home", "title": "Home", "text": "Welcome a@b.c", "tree": "- e1 link Statements", "elements": []}


@pytest.fixture
def ctx(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(fb, "STORE", store)
    calls: dict[str, list] = {"act": [], "nav": [], "plan": [], "internal": []}

    async def nav(c, url):
        calls["nav"].append(url)
        return dict(LOGIN)

    async def snap(c):
        return dict(LOGIN)

    monkeypatch.setattr(fb, "_navigate", nav)
    monkeypatch.setattr(fb, "_snapshot", snap)
    c = SimpleNamespace(user=SimpleNamespace(id=uuid.uuid4()), store=store, calls=calls)
    return c


def _plans(monkeypatch, ctx, plans):
    queue = list(plans)

    async def plan_step(model, goal, state, values, last, allow_final):
        ctx.calls["plan"].append({"model": model, "brief": fb.page_brief(state), "values": dict(values)})
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(fb, "plan_step", plan_step)


async def test_browser_task_batches_and_saves_a_trace_without_values(monkeypatch, ctx, caplog):
    caplog.set_level(logging.INFO)
    _plans(monkeypatch, ctx, [
        {"actions": [{"kind": "fill", "ref": "e1", "text": "{{email}}"}, {"kind": "fill", "ref": "e2", "text": "{{password}}"}, {"kind": "click", "ref": "e3"}], "done": True, "result": "Signed in as a@b.c", "need_user": ""},
    ])  # fmt: skip

    async def act(c, actions):
        ctx.calls["act"].append(actions)
        trace = [{"i": 0, "target": {"name_attr": "email"}}, {"i": 1, "target": {"input_type": "password", "name_attr": "password"}, "secret": True},
                 {"i": 2, "target": {"role": "button", "name": "Sign in"}}]  # fmt: skip
        return {**HOME, "completed": 3, "trace": trace}

    monkeypatch.setattr(fb, "_act", act)
    out = await fb.browser_task(ctx, "Log in to Acme", "https://acme.test/login", {"email": "a@b.c", "password": "hunter22"})
    assert out["ok"] and out["done"] and out["via"] == "model"
    assert len(ctx.calls["plan"]) == 1 and len(ctx.calls["act"]) == 1  # one model call, one batch
    assert ctx.calls["act"][0][1]["text"] == "hunter22"  # substituted right before the daemon types
    assert all("hunter22" not in p["brief"] for p in ctx.calls["plan"])
    blob = json.dumps(out) + json.dumps(ctx.store.saved) + "\n".join(r.getMessage() for r in caplog.records)
    assert "hunter22" not in blob and "a@b.c" not in json.dumps(out)
    saved = ctx.store.saved[0]
    assert saved["host"] == "acme.test" and [s["kind"] for s in saved["steps"]] == ["fill", "fill", "click"]


async def test_browser_task_escalates_then_falls_back(monkeypatch, ctx):
    monkeypatch.setattr(get_settings(), "browser_fast_model", "fast-x")
    monkeypatch.setattr(get_settings(), "browser_escalate_model", "strong-y")
    _plans(monkeypatch, ctx, [None, RuntimeError("model down"), None, None])

    async def act(c, actions):
        raise AssertionError("no plan, no actions")

    monkeypatch.setattr(fb, "_act", act)
    out = await fb.browser_task(ctx, "Do it", "https://acme.test/login", {})
    assert out["fallback"] is True and out["ok"] is False and out["page"]["url"] == "https://acme.test/login"
    assert [p["model"] for p in ctx.calls["plan"]] == ["fast-x", "fast-x", "strong-y", "strong-y"]


async def test_browser_task_stops_before_final_actions(monkeypatch, ctx):
    state = {**LOGIN, "elements": [{"ref": "e9", "name": "Pay now"}]}

    async def snap(c):
        return state

    monkeypatch.setattr(fb, "_snapshot", snap)
    _plans(monkeypatch, ctx, [{"actions": [{"kind": "click", "ref": "e9"}], "done": False, "result": "", "need_user": ""}])
    out = await fb.browser_task(ctx, "pay the bill", "", {})
    assert out["stopped_before"] == "Pay now" and out["ok"] is True


async def test_browser_task_reports_missing_values(monkeypatch, ctx):
    _plans(monkeypatch, ctx, [{"actions": [{"kind": "fill", "ref": "e1", "text": "{{otp}}"}], "done": False, "result": "", "need_user": ""}])

    async def act(c, actions):
        raise AssertionError("must not type a raw placeholder")

    monkeypatch.setattr(fb, "_act", act)
    out = await fb.browser_task(ctx, "enter code", "https://acme.test/login", {})
    assert out["need_user"] == "Missing values: otp"


async def test_browser_task_replays_a_saved_trace_without_model_calls(monkeypatch, ctx):
    ctx.store.rows = [{"id": "t1", "host": "acme.test", "name": "log in to acme", "goal": "Log in to Acme", "start_url": "https://acme.test/login",
                       "steps": [{"kind": "fill", "target": {"name_attr": "email"}, "param": "email"}, {"kind": "click", "target": {"role": "button", "name": "Sign in"}}]}]  # fmt: skip
    _plans(monkeypatch, ctx, [])
    sent = []

    async def internal(c, name, args):
        sent.append((name, args))
        return {**HOME, "completed": len(args["steps"])}

    monkeypatch.setattr(fb, "_internal", internal)
    out = await fb.browser_task(ctx, "log in to acme", "https://acme.test/login", {"email": "a@b.c"})
    assert out["via"] == "replay" and out["done"] and ctx.calls["plan"] == []
    assert sent[0][0] == "browser_replay" and sent[0][1]["steps"][0]["text"] == "a@b.c"
    assert ctx.store.marks == [("t1", True)]


async def test_replay_mismatch_falls_back_to_the_model(monkeypatch, ctx):
    ctx.store.rows = [{"id": "t1", "host": "acme.test", "name": "log in to acme", "goal": "log in to acme", "start_url": "https://acme.test/login",
                       "steps": [{"kind": "click", "target": {"role": "button", "name": "Old button"}}]}]  # fmt: skip

    async def internal(c, name, args):
        return {**LOGIN, "ok": False, "mismatch": True, "completed": 0, "error": "step 1: target not found"}

    monkeypatch.setattr(fb, "_internal", internal)
    _plans(monkeypatch, ctx, [{"actions": [], "done": True, "result": "already signed in", "need_user": ""}])
    out = await fb.browser_task(ctx, "log in to acme", "https://acme.test/login", {})
    assert out["via"] == "model" and out["done"] and ctx.store.marks == [("t1", False)]
    assert len(ctx.calls["plan"]) == 1


async def test_browser_task_never_raises(monkeypatch, ctx):
    async def nav(c, url):
        raise RuntimeError("boom hunter22")

    monkeypatch.setattr(fb, "_navigate", nav)
    monkeypatch.setattr(fb, "STORE", FakeStore())
    out = await fb.browser_task(ctx, "x", "https://acme.test/", {"password": "hunter22"})
    assert out["fallback"] is True and "hunter22" not in json.dumps(out)


async def test_browser_task_lets_stop_through(monkeypatch, ctx):
    from app.agent import cancel as run_cancel

    async def nav(c, url):
        raise run_cancel.TurnCancelled("stopped")

    monkeypatch.setattr(fb, "_navigate", nav)
    with pytest.raises(run_cancel.TurnCancelled):
        await fb.browser_task(ctx, "x", "https://acme.test/", {})


async def test_replay_named_reports_missing_values_and_unknown_runs(monkeypatch, ctx):
    ctx.store.rows = [{"id": "t1", "host": "acme.test", "name": "x [skill abc]", "goal": "", "start_url": "https://acme.test/", "skill_id": "abc",
                       "steps": [{"kind": "fill", "target": {"input_type": "password"}, "param": "password", "secret": True}], "params": [{"name": "password", "secret": True}]}]  # fmt: skip
    out = await fb.replay_named(ctx, skill_id="abc")
    assert out["missing_values"] == ["password"]
    none = await fb.replay_named(ctx, skill_id="zzz")
    assert none["ok"] is False and "no saved run" in none["error"]


async def test_browser_download_fetches_directly_then_falls_back_to_click(monkeypatch, ctx):
    from app.agent import computer_harness as computer

    async def internal(c, name, args):
        assert name == "browser_download"
        return {"ok": False, "error": "the link returns a web page, not a file"}

    clicked = []

    async def run_computer(c, name, args):
        clicked.append((name, args))
        return {"ok": True, "downloads": [{"name": "s.pdf"}]}

    monkeypatch.setattr(fb, "_internal", internal)
    monkeypatch.setattr(computer, "run_computer", run_computer)
    out = await fb.browser_download(ctx, url="https://acme.test/s", ref="e5")
    assert out["ok"] and clicked == [("browser_act", {"actions": [{"kind": "download", "ref": "e5", "url": "https://acme.test/s"}]})]


def test_prompt_hint_follows_flags(monkeypatch):
    assert "browser_task" in fb.prompt_hint() and "browser_replay" in fb.prompt_hint()
    for flag in ("browser_fast_loop", "browser_replay", "browser_direct_downloads"):
        monkeypatch.setattr(get_settings(), flag, False)
    hint = fb.prompt_hint()
    assert "browser_task" not in hint and "browser_replay" not in hint and "batch" in hint


def test_harness_tools_follow_flags(monkeypatch):
    from app.agent.harness_extra import extra_harness_tools

    ctx = SimpleNamespace(used_tools=[], connectors={}, user=None)
    names = {t.name for t in extra_harness_tools(ctx)}
    assert {"browser_task", "browser_replay", "browser_download"} <= names
    for flag in ("browser_fast_loop", "browser_replay", "browser_direct_downloads"):
        monkeypatch.setattr(get_settings(), flag, False)
    names = {t.name for t in extra_harness_tools(ctx)}
    assert not names & {"browser_task", "browser_replay", "browser_download"}
    assert "browser_act" in names


async def test_llm_model_override(monkeypatch):
    from app import llm

    seen = {}

    class Responses:
        async def create(self, **kw):
            seen.update(kw)
            raise RuntimeError("stop here")

    monkeypatch.setattr(llm, "openai_client", lambda *a, **k: SimpleNamespace(responses=Responses()))
    with pytest.raises(Exception):
        await llm.chat_complete([{"role": "user", "content": "hi"}], model="tiny-model")
    assert seen.get("model") == "tiny-model"


# --- teach mode: DOM recorder -> replay trace ---------------------------------------------------------------------
async def test_teach_dom_tick_starts_then_drains(monkeypatch):
    from app.skills import recorder

    replies = [{"ok": True, "recording": True}, {"ok": True, "recording": True, "events": [{"kind": "click", "target": {"id": "go"}}]}]
    ops = []

    async def dom(rec, op):
        ops.append(op)
        return replies.pop(0)

    monkeypatch.setattr(recorder, "_dom", dom)
    rec = recorder.Recorder(user_key=str(uuid.uuid4()), started=0.0)
    await recorder._dom_tick(rec)
    await recorder._dom_tick(rec)
    assert ops == ["start", "drain"] and rec.dom_on and rec.dom_events == [{"kind": "click", "target": {"id": "go"}}]
    monkeypatch.setattr(get_settings(), "browser_teach_dom", False)
    await recorder._dom_tick(rec)
    assert ops == ["start", "drain"]


async def test_teach_trace_is_saved_for_the_skill(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(fb, "STORE", store)
    events = [{"kind": "fill", "url": "https://acme.test/login", "target": {"name_attr": "email"}, "text": "a@b.c"},
              {"kind": "click", "url": "https://acme.test/login", "target": {"role": "button", "name": "Sign in"}}]  # fmt: skip
    assert await fb.save_teach_trace(uuid.uuid4(), "abc12345-skill", "Log in to Acme", events) == "t-new"
    saved = store.saved[0]
    assert saved["source"] == "teach" and saved["skill_id"] == "abc12345-skill" and saved["host"] == "acme.test"
    assert 'browser_replay(skill_id="abc12345-skill"' in fb.teach_replay_line("abc12345-skill")
    assert await fb.save_teach_trace(uuid.uuid4(), "s", "x", []) is None
