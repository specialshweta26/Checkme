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

    busy = False
    busy_checks = 0

    def evaluate(self, js, arg=None):
        if "aria-busy" in str(js):
            self.busy_checks += 1
            return self.busy
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

    async def propose(self, trace_id, steps, note):
        self.proposals = getattr(self, "proposals", []) + [(trace_id, steps, note)]
        return True


LOGIN = {
    "ok": True, "url": "https://acme.test/login", "title": "Sign in", "text": "Sign in to Acme",
    "tree": "- e1 textbox Email [email]\n- e2 textbox Password [password]\n- e3 button Sign in",
    "elements": [{"ref": "e1", "name": "Email"}, {"ref": "e2", "name": "Password"}, {"ref": "e3", "name": "Sign in"}],
}  # fmt: skip
HOME = {"ok": True, "url": "https://acme.test/home", "title": "Home", "text": "Welcome a@b.c", "tree": "- e1 link Statements", "elements": []}


@pytest.fixture(autouse=True)
def no_saved_logins(monkeypatch):
    from app.browser_runs import logins

    async def none(user_id, host):
        return {}

    monkeypatch.setattr(logins, "values_for", none)


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
    monkeypatch.setattr(get_settings(), "browser_heal", False)
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


def test_page_brief_keeps_the_end_of_a_long_page_and_a_draft():
    brief = fb.page_brief({"text": "sidebar " + ("x" * 4000) + " Hello", "composer": "Hello", "tree": "- e0 textbox"}, text_chars=800)
    text = brief.split("Text:\n", 1)[1].split("\nElements:", 1)[0]
    assert "Composer still holds: Hello" in brief
    assert "\n…\n" in text and text.rstrip().endswith("Hello")


@pytest.mark.parametrize("name", ["shipping_address", "passenger_name", "promo_code", "zip_code", "country_code", "pincode", "compassion", "spin", "laptop", "hotpot", "shippingAddress", "passengerName"])
def test_ordinary_fields_are_not_secret(name):
    assert not d.looks_secret(name) and not fb.secret_param(name)
    assert not d.is_secret_target({"name_attr": name})


@pytest.mark.parametrize("name", ["password", "userPassword", "userpassword", "user_pass", "pass", "passwd", "pwd", "otp", "smsOtp", "emailotp", "enterOTP", "cvv", "cc-csc", "cc-number", "cardNumber", "card_number", "one-time-code", "security code", "verification_code", "pin", "mpin", "api_token", "client_secret", "passport_number"])
def test_secret_fields_are_detected(name):
    assert d.looks_secret(name) and fb.secret_param(name)


def test_a_bare_code_placeholder_is_secret_but_only_as_a_placeholder():
    assert fb.secret_param("code") and not d.looks_secret("code")


def test_recorder_script_uses_the_same_secret_pattern():
    assert "const secretRe = /" + d._SECRET_PATTERN + "/;" in d._REC_JS


@pytest.mark.parametrize("label", ["Send", "Send message", "Confirm order", "Proceed to payment", "Submit payment", "Book flight", "Cancel my order", "भुगतान करें", "अभी खरीदें", "भेजें"])
def test_final_click_catches_more_final_labels(label):
    state = {"elements": [{"ref": "e1", "name": label, "role": "button"}]}
    assert fb.final_click([{"kind": "click", "ref": "e1"}], state) == label


@pytest.mark.parametrize("label", ["Send code", "Send OTP", "Send me a link", "Resend", "Continue", "Proceed", "Next", "Checkout"])
def test_final_click_leaves_ordinary_steps_alone(label):
    state = {"url": "https://acme.test/login", "text": "Sign in", "elements": [{"ref": "e1", "name": label, "role": "button"}]}
    assert fb.final_click([{"kind": "click", "ref": "e1"}], state) == ""


@pytest.mark.parametrize("page", [{"text": "Amount payable: Rs 500. Enter CVV"}, {"url": "https://acme.test/payment/step2"}])
def test_final_click_is_stricter_on_a_payment_page(page):
    state = {"url": "https://acme.test/x", "text": "", **page, "elements": [
        {"ref": "e1", "name": "Proceed", "role": "button"},
        {"ref": "e2", "name": "", "role": "button"},
        {"ref": "e3", "name": "", "role": "textbox"},
        {"ref": "e4", "name": "Help", "role": "link"},
    ]}  # fmt: skip
    assert fb.final_click([{"kind": "click", "ref": "e1"}], state) == "Proceed"
    assert "unlabeled" in fb.final_click([{"kind": "click", "ref": "e2"}], state)
    assert "Enter" in fb.final_click([{"kind": "press", "text": "Enter"}], state)
    assert fb.final_click([{"kind": "press", "text": "Tab"}], state) == ""
    assert fb.final_click([{"kind": "click", "ref": "e3"}], state) == ""
    assert fb.final_click([{"kind": "click", "ref": "e4"}], state) == ""


def test_trace_final_step_reads_saved_targets():
    assert fb.trace_final_step([{"kind": "click", "target": {"role": "button", "name": "Pay now"}}]) == "Pay now"
    assert fb.trace_final_step([{"kind": "click", "target": {"aria_label": "Place order"}}]) == "Place order"
    assert fb.trace_final_step([{"kind": "click", "target": {"name": "Sign in"}}, {"kind": "fill", "target": {"name": "Pay"}}]) == ""


FINAL_TRACE = {"id": "t9", "host": "acme.test", "name": "pay the bill", "goal": "Pay the bill", "start_url": "https://acme.test/bill",
               "steps": [{"kind": "click", "target": {"role": "button", "name": "Pay now"}}]}  # fmt: skip


async def test_browser_task_does_not_replay_a_saved_final_action(monkeypatch, ctx):
    ctx.store.rows = [dict(FINAL_TRACE)]
    state = {**LOGIN, "elements": [{"ref": "e9", "name": "Pay now"}]}

    async def snap(c):
        return state

    async def replay(*a, **k):
        raise AssertionError("must not replay a saved final action")

    monkeypatch.setattr(fb, "_snapshot", snap)
    monkeypatch.setattr(fb, "replay", replay)
    _plans(monkeypatch, ctx, [{"actions": [{"kind": "click", "ref": "e9"}], "done": False, "result": "", "need_user": ""}])
    out = await fb.browser_task(ctx, "Pay the bill", "", {})
    assert out["stopped_before"] == "Pay now"


async def test_replay_named_refuses_a_saved_final_action(monkeypatch, ctx):
    ctx.store.rows = [dict(FINAL_TRACE)]

    async def replay(*a, **k):
        raise AssertionError("must not replay a saved final action")

    monkeypatch.setattr(fb, "replay", replay)
    out = await fb.replay_named(ctx, "pay the bill", "https://acme.test/bill")
    assert out["ok"] is False and out["stopped_before"] == "Pay now"


async def test_replay_named_runs_a_final_action_the_user_asked_for(monkeypatch, ctx):
    ctx.store.rows = [dict(FINAL_TRACE)]
    ran = []

    async def replay(c, trace, values, **k):
        ran.append(trace["id"])
        return {"ok": True, "completed": 1, "state": {}}

    monkeypatch.setattr(fb, "replay", replay)
    out = await fb.replay_named(ctx, "pay the bill", "https://acme.test/bill", allow_final_action=True)
    assert out["ok"] is True and ran == ["t9"]


async def test_browser_task_replays_a_final_action_the_user_asked_for(monkeypatch, ctx):
    ctx.store.rows = [dict(FINAL_TRACE)]
    ran = []

    async def replay(c, trace, values, **k):
        ran.append(trace["id"])
        return {"ok": True, "completed": 1, "state": {}}

    monkeypatch.setattr(fb, "replay", replay)
    _plans(monkeypatch, ctx, [])
    out = await fb.browser_task(ctx, "Pay the bill", "https://acme.test/bill", {}, allow_final_action=True)
    assert out["via"] == "replay" and ran == ["t9"]


@pytest.mark.parametrize("label", ["Download", "Download statement", "Export PDF"])
def test_downloads_are_never_treated_as_final(label):
    state = {"url": "https://acme.test/payment/x", "text": "Amount payable", "elements": [{"ref": "e1", "name": label, "role": "button"}]}
    assert fb.final_click([{"kind": "click", "ref": "e1"}], state) == ""
    assert fb.trace_final_step([{"kind": "click", "target": {"role": "button", "name": label}}]) == ""


def test_browser_replay_tool_accepts_the_permission():
    from app.agent.harness_extra import BrowserReplayInput

    assert BrowserReplayInput(allow_final_action=True).allow_final_action is True
    assert BrowserReplayInput().allow_final_action is False


# --- healing saved runs (docs/BROWSING.md "Healing saved runs") ---------------------------------------------------
REPORT_TRACE = {
    "id": "t5", "host": "acme.test", "name": "download order status", "goal": "Download the order status report",
    "start_url": "https://acme.test/reports",
    "steps": [
        {"kind": "click", "target": {"role": "link", "name": "Reporting"}},
        {"kind": "click", "target": {"role": "button", "name": "Old Order Status"}},
        {"kind": "fill", "target": {"role": "textbox", "label": "From"}, "param": "from_date"},
    ],
}  # fmt: skip


def test_describe_step_reads_like_english():
    steps = REPORT_TRACE["steps"]
    assert fb.describe_step(steps[0]) == 'click the link "Reporting"'
    assert fb.describe_step(steps[2]) == 'fill the textbox "From" with {{from_date}}'
    assert fb.describe_step({"kind": "navigate", "url": "https://a.test/"}) == "open https://a.test/"
    assert fb.describe_step({"kind": "hover", "target": {"role": "link", "name": "WMS"}}) == 'hover the link "WMS"'


async def test_replay_heals_a_changed_step_and_proposes_the_fix(monkeypatch, ctx):
    ctx.store.rows = [dict(REPORT_TRACE)]
    sent = []

    async def internal(c, name, args):
        sent.append([s.get("target", {}).get("name") or s.get("target", {}).get("label") for s in args["steps"]])
        if len(sent) == 1:  # step 2's button was renamed
            return {**HOME, "ok": False, "mismatch": True, "completed": 1, "error": "mismatch: the recorded element is not on this page"}
        return {**HOME, "completed": len(args["steps"])}

    healed_with = {"kind": "click", "target": {"role": "button", "name": "Order Status Report"}}
    asked = []

    async def task(c, goal, url="", values=None, max_steps=0, allow_final_action=False, use_saved=True, save=True):
        asked.append({"goal": goal, "save": save, "use_saved": use_saved, "max_steps": max_steps})
        return {"ok": True, "done": True, "_recorded": [healed_with], "page": {}}

    monkeypatch.setattr(fb, "_internal", internal)
    monkeypatch.setattr(fb, "browser_task", task)
    out = await fb.replay_named(ctx, "download order status", "https://acme.test/reports", values={"from_date": "2026-05-01"})
    assert out["ok"] is True and out["proposed_fix"] is True
    # Only the broken step went to the model, and it was told not to do later steps or save a run.
    assert len(asked) == 1 and 'click the button "Old Order Status"' in asked[0]["goal"] and "Do not do any later step" in asked[0]["goal"]
    assert asked[0]["save"] is False and asked[0]["use_saved"] is False and asked[0]["max_steps"] == fb.HEAL_ROUNDS
    # The replay carried on after the fix with the remaining step.
    assert sent == [["Reporting", "Old Order Status", "From"], ["From"]]
    trace_id, steps, note = ctx.store.proposals[0]
    assert trace_id == "t5" and steps == [REPORT_TRACE["steps"][0], healed_with, REPORT_TRACE["steps"][2]]
    assert note[0]["step"] == 2 and note[0]["now"] == ['click the button "Order Status Report"']
    assert "unchanged until the user accepts" in out["heal_note"]
    # A healed run does not count as a clean success.
    assert ctx.store.marks == [("t5", False)]


async def test_replay_stops_when_healing_fails(monkeypatch, ctx):
    ctx.store.rows = [dict(REPORT_TRACE)]

    async def internal(c, name, args):
        return {**HOME, "ok": False, "mismatch": True, "completed": 0, "error": "mismatch"}

    async def task(*a, **k):
        return {"ok": False, "done": False, "fallback": True, "page": {}}

    monkeypatch.setattr(fb, "_internal", internal)
    monkeypatch.setattr(fb, "browser_task", task)
    out = await fb.replay_named(ctx, "download order status", "https://acme.test/reports", values={"from_date": "x"})
    assert out["ok"] is False and out["mismatch"] is True and not getattr(ctx.store, "proposals", [])


async def test_replay_heals_at_most_max_heals(monkeypatch, ctx):
    trace = {**REPORT_TRACE, "steps": [{"kind": "click", "target": {"role": "button", "name": f"B{i}"}} for i in range(5)]}
    calls = []

    async def internal(c, name, args):
        return {**HOME, "ok": False, "mismatch": True, "completed": 0, "error": "mismatch"}

    async def task(*a, **k):
        calls.append(1)
        return {"ok": True, "done": True, "_recorded": [{"kind": "click", "target": {"role": "button", "name": "new"}}], "page": {}}

    monkeypatch.setattr(fb, "_internal", internal)
    monkeypatch.setattr(fb, "browser_task", task)
    out = await fb.replay(ctx, trace, {}, heal=True)
    assert out["ok"] is False and len(calls) == fb.MAX_HEALS and len(out["healed"]) == fb.MAX_HEALS


async def test_browser_task_save_false_returns_steps_instead_of_saving(monkeypatch, ctx):
    _plans(monkeypatch, ctx, [{"actions": [{"kind": "click", "ref": "e3"}], "done": True, "result": "clicked", "need_user": ""}])

    async def act(c, actions):
        return {**HOME, "completed": 1, "trace": [{"i": 0, "target": {"role": "button", "name": "Sign in"}}]}

    monkeypatch.setattr(fb, "_act", act)
    out = await fb.browser_task(ctx, "click sign in", "", {}, use_saved=False, save=False)
    assert out["_recorded"] == [{"kind": "click", "target": {"role": "button", "name": "Sign in"}}]
    assert ctx.store.saved == []


async def test_fast_loop_counts_a_no_effect_batch_and_says_so(monkeypatch, ctx):
    _plans(monkeypatch, ctx, [
        {"actions": [{"kind": "click", "ref": "e3"}], "done": False, "result": "", "need_user": ""},
        {"actions": [], "done": True, "result": "ok", "need_user": ""},
    ])  # fmt: skip

    async def act(c, actions):
        return {**LOGIN, "completed": 1, "trace": [{"i": 0, "target": {"name": "Sign in"}}], "warnings": ["step 1 (click e3) may not have done anything"]}

    monkeypatch.setattr(fb, "_act", act)
    out = await fb.browser_task(ctx, "sign in", "https://acme.test/login", {})
    assert out["done"] is True
    second = ctx.calls["plan"][1]["brief"]
    assert second  # the planner was called again after the no-op


async def test_fast_loop_find_relists_the_page(monkeypatch, ctx):
    _plans(monkeypatch, ctx, [
        {"actions": [{"kind": "find", "text": "Order Status"}], "done": False, "result": "", "need_user": ""},
        {"actions": [], "done": True, "result": "found", "need_user": ""},
    ])  # fmt: skip
    finds = []

    async def snap(c, find=""):
        finds.append(find)
        return dict(LOGIN)

    monkeypatch.setattr(fb, "_snapshot", snap)
    out = await fb.browser_task(ctx, "find the report", "https://acme.test/login", {})
    assert out["done"] and "Order Status" in finds


# --- reliability: empty pages, "needs you" that isn't, bot checks (from real MakeMyTrip runs) ----------------------

@pytest.mark.parametrize(
    "text, kind",
    [
        ("Please provide the current page URL/title/visible text and the Elements list.", "page"),
        ("Once the flights page loads, I'll need the visible form elements.", "page"),
        ("I need you to confirm the date picker interaction: please select 21 Nov in the calendar.", "page"),
        ("Please confirm you're okay with using Google Flights (or tell me another airline/website you prefer).", "site_choice"),
        ("The site shows a CAPTCHA.", "real"),
        ("Enter the one-time code sent to your phone.", "real"),
        ("Sign in to continue.", "real"),
        ("Two flights at 9:05 and 9:40 are the same price: which one?", "real"),
        ("Something unexpected happened.", "real"),  # unknown: stay safe and stop
    ],
)
def test_need_user_kinds(text, kind):
    assert fb.need_user_kind(text) == kind


EMPTY = {"ok": True, "url": "https://www.makemytrip.com/flights/", "title": "", "text": "", "tree": "", "elements": []}


async def test_an_empty_page_is_read_again_before_planning(monkeypatch, ctx):
    monkeypatch.setattr(fb, "SETTLE_WAITS_S", (0, 0, 0))
    pages = [dict(EMPTY), dict(EMPTY), dict(LOGIN)]

    async def snap(c, find=""):
        return pages.pop(0) if pages else dict(LOGIN)

    monkeypatch.setattr(fb, "_snapshot", snap)
    _plans(monkeypatch, ctx, [{"actions": [], "done": True, "result": "Sign-in form is ready", "need_user": ""}])
    out = await fb.browser_task(ctx, "Find the sign-in form", "", {})
    assert out["done"] and "e1 textbox Email" in ctx.calls["plan"][0]["brief"]  # planned on the loaded page, not the empty one


async def test_asking_the_user_for_page_details_does_not_end_the_run(monkeypatch, ctx):
    monkeypatch.setattr(fb, "SETTLE_WAITS_S", (0,))
    _plans(monkeypatch, ctx, [
        {"actions": [], "done": False, "result": "", "need_user": "Please share the Elements list after the page loads."},
        {"actions": [], "done": False, "result": "", "need_user": "Please confirm you're okay with using Google Flights."},
        {"actions": [], "done": True, "result": "Found 3 flights", "need_user": ""},
    ])
    out = await fb.browser_task(ctx, "Search flights", "", {})
    assert out.get("done"), out
    assert out["result"] == "Found 3 flights" and len(ctx.calls["plan"]) == 3


async def test_a_real_human_step_still_stops(monkeypatch, ctx):
    _plans(monkeypatch, ctx, [{"actions": [], "done": False, "result": "", "need_user": "The site shows a CAPTCHA."}])
    out = await fb.browser_task(ctx, "Search flights", "", {})
    assert out["need_user"] == "The site shows a CAPTCHA." and not out["done"]


async def test_a_bot_check_in_the_cloud_retries_on_the_users_desktop(monkeypatch, ctx):
    from app.desktop import client

    seen_kinds = []
    calls = {"n": 0}

    async def task(c, goal, url, values, max_steps, allow_final, use_saved, save):
        calls["n"] += 1
        seen_kinds.append(client.client_kind())
        if calls["n"] == 1:
            return {"ok": False, "done": False, "need_user": fb.BOT_WALL_NEED, "page": {}}
        return {"ok": True, "done": True, "result": "3 flights", "page": {}}

    async def online(c):
        return True

    monkeypatch.setattr(fb, "_browser_task", task)
    monkeypatch.setattr(fb, "desktop_available", online)
    monkeypatch.setattr(get_settings(), "browser_run_log", False)  # the wrapper's retry needs the run-log path
    monkeypatch.setattr(get_settings(), "browser_run_log", True)
    from app.browser_runs import runlog

    class Run:
        def __init__(self, *a):
            self.host, self._saved, self.id = "", False, uuid.uuid4()

        async def step(self, *a, **k):
            pass

        async def finish(self, *a, **k):
            pass

    monkeypatch.setattr(runlog, "RunLog", Run)
    out = await fb.browser_task(ctx, "Search flights on MakeMyTrip", "https://www.makemytrip.com/", {})
    assert out["done"] and out["ran_on"] == "desktop" and "desktop app" in out["note"]
    assert seen_kinds[0] != client.CLIENT_DESKTOP and seen_kinds[1] == client.CLIENT_DESKTOP


async def test_no_desktop_retry_when_offline_or_cloud_only(monkeypatch, ctx):
    async def task(c, *a):
        return {"ok": False, "done": False, "need_user": fb.BOT_WALL_NEED, "page": {}}

    async def offline(c):
        return False

    monkeypatch.setattr(fb, "_browser_task", task)
    monkeypatch.setattr(fb, "desktop_available", offline)
    out = await fb.browser_task(ctx, "Search flights", "https://www.makemytrip.com/", {})
    assert out["need_user"] == fb.BOT_WALL_NEED and "ran_on" not in out
