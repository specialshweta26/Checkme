"""Generic browser basics: date fields, values that must stick, loading indicators, clicks that change nothing,
and long element lists (docs/BROWSING.md)."""

import datetime as dt

import pytest

from app.agent import fast_browser as fast
from app.connectors import computer_daemon as d


# --- dates ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    ["2026-05-13", "13/05/2026", "13-05-2026", "13.05.2026", "13 May 2026", "13th May, 2026", "May 13, 2026",
     "05/13/2026", "Wed 13 May 2026", "2026-05-13T09:30"],
)  # fmt: skip
def test_parse_date_reads_common_formats(text):
    assert d.parse_date(text) == dt.date(2026, 5, 13)


def test_parse_date_is_day_first_when_both_readings_work():
    assert d.parse_date("03/04/2026") == dt.date(2026, 4, 3)
    assert d.parse_date("not a date") is None


def test_native_date_values_are_iso():
    assert d.native_date_value("13 May 2026", "date") == "2026-05-13"
    assert d.native_date_value("13/05/2026 9:30 pm", "datetime-local") == "2026-05-13T21:30"
    assert d.native_date_value("May 2026", "month") == "2026-05"
    assert d.native_date_value("12:15 am", "time") == "00:15"
    assert d.native_date_value("2026-W20", "week") == "2026-W20"
    assert d.native_date_value("soon", "date") is None


def test_date_field_kind():
    assert d.date_field_kind({"input_type": "date"}) == "native"
    assert d.date_field_kind({}, {"type": "datetime-local"}) == "native"
    assert d.date_field_kind({"input_type": "text", "placeholder": "DD/MM/YYYY"}) == "text"
    assert d.date_field_kind({"label": "From date"}) == "text"
    assert d.date_field_kind({}, {"type": "text", "hint": "react-datepicker__input"}) == "text"
    assert d.date_field_kind({"input_type": "email", "label": "Date of contact"}) == ""
    assert d.date_field_kind({"label": "Update notes"}) == ""


def test_value_kept_tolerates_masks_and_reformatted_dates():
    assert d.value_kept("9876543210", "98765 43210")
    assert d.value_kept("hello", "hello")
    assert not d.value_kept("hello", "")
    assert not d.value_kept("Bengaluru", "B")
    assert d.value_kept("2026-05-13", "13/05/2026", date_like=True)
    assert not d.value_kept("2026-05-13", "14/05/2026", date_like=True)


class _Keys:
    def __init__(self):
        self.pressed, self.typed = [], []

    def press(self, key):
        self.pressed.append(key)

    def type(self, text, delay=0):
        self.typed.append(text)


class _Page:
    def __init__(self):
        self.keyboard = _Keys()
        self.url = "https://a.test/"

    def wait_for_timeout(self, ms):
        return None


class _Field:
    """An <input>. ``sticky=False`` drops whatever fill() sets, like a React-controlled field."""

    def __init__(self, typ="text", sticky=True, hint=""):
        self.typ, self.sticky, self.hint = typ, sticky, hint
        self.value, self.filled, self.clicks = "", [], 0

    def evaluate(self, js):
        return {"role": "", "ac": "", "popup": "", "list": "", "tag": "input", "ce": False, "type": self.typ, "hint": self.hint}

    def fill(self, text, timeout=0):
        self.filled.append(text)
        if self.sticky:
            self.value = text

    def input_value(self, timeout=0):
        return self.value

    def click(self, timeout=0):
        self.clicks += 1


def test_native_date_input_gets_iso():
    page, loc = _Page(), _Field("date")
    d._type_into(page, loc, {"tag": "input", "input_type": "date"}, "13 May 2026", replace=True)
    assert loc.filled == ["2026-05-13"] and page.keyboard.typed == []


def test_native_date_input_refuses_an_unreadable_date():
    with pytest.raises(RuntimeError, match="YYYY-MM-DD"):
        d._type_into(_Page(), _Field("date"), {"input_type": "date"}, "next Tuesday-ish", replace=True)


def test_value_that_did_not_stick_is_typed_like_a_person():
    page, loc = _Page(), _Field(sticky=False)
    d._type_into(page, loc, {"tag": "input", "role": "textbox"}, "Order Status", replace=True)
    assert loc.filled == ["Order Status"] and page.keyboard.typed == ["Order Status"]
    assert "Tab" not in page.keyboard.pressed


def test_date_text_box_gets_tab_to_commit_the_picker():
    page, loc = _Page(), _Field(hint="DD/MM/YYYY")
    d._type_into(page, loc, {"tag": "input", "role": "textbox", "placeholder": "DD/MM/YYYY"}, "13/05/2026", replace=True)
    assert page.keyboard.typed == [] and page.keyboard.pressed[-1] == "Tab"


def test_password_field_is_never_read_back():
    page, loc = _Page(), _Field("password", sticky=False)
    d._type_into(page, loc, {"tag": "input", "input_type": "password"}, "hunter22", replace=True)
    assert page.keyboard.typed == []


# --- loading indicators and no-op clicks --------------------------------------------------------------------
class _BusyPage:
    def __init__(self, busy):
        self.busy, self.waited = busy, []

    def evaluate(self, js, arg=None):
        return self.busy

    def wait_for_function(self, js, timeout=0, polling=0):
        self.waited.append(timeout)


def test_wait_not_busy_only_waits_when_a_spinner_shows():
    idle = _BusyPage(False)
    assert d.wait_not_busy(idle) == 0 and idle.waited == []
    busy = _BusyPage(True)
    d.wait_not_busy(busy, 3000)
    assert busy.waited == [3000]


def test_no_effect_note_names_the_step():
    note = d.no_effect_note(1, "click", "e7")
    assert "step 2 (click e7)" in note and "hover" in note


class _SigPage:
    def __init__(self, sigs):
        self.sigs = list(sigs)

    def evaluate(self, js, arg=None):
        return self.sigs.pop(0) if self.sigs else None


def test_page_signature_survives_script_errors():
    class Broken:
        def evaluate(self, js, arg=None):
            raise RuntimeError("navigating")

    assert d.page_signature(Broken()) is None
    assert d.page_signature(_SigPage(["a|1"])) == "a|1"


def test_signature_script_counts_what_a_click_changes():
    for part in ("location.href", "aria-expanded", "input:checked", "scrollY"):
        assert part in d._PAGE_SIG_JS


# --- long element lists -------------------------------------------------------------------------------------
def test_omitted_line_points_at_find():
    assert d.omitted_line(0) == ""
    line = d.omitted_line(37)
    assert "37 more elements" in line and "find=" in line


def test_snapshot_script_takes_find_and_limit():
    js = d._SNAPSHOT_JS
    assert "opts.find" in js and "opts.limit" in js and "__checkmeSnapTotal" in js
    assert "ranked.slice(0, limit)" in js


def test_max_elements_is_bounded(monkeypatch):
    monkeypatch.setattr(d, "CONFIG", {"max_elements": 5000})
    assert d.max_elements() == 400
    monkeypatch.setattr(d, "CONFIG", {})
    assert d.max_elements() == d.MAX_ELEMENTS


def test_page_brief_keeps_the_omitted_note_when_the_tree_is_cut():
    tree = "\n".join(f"- e{i} link Item {i}" for i in range(400))
    brief = fast.page_brief({"url": "u", "title": "t", "text": "", "tree": tree, "elements_omitted": 250}, tree_chars=500)
    assert "250 more elements are not listed (use find)" in brief


def test_fast_plan_accepts_find():
    plan = fast.parse_plan('{"actions": [{"kind": "find", "text": "Order Status"}], "done": false}')
    assert plan["actions"] == [{"kind": "find", "text": "Order Status"}]
    assert "find" in fast.FAST_SYSTEM and "YYYY-MM-DD" in fast.FAST_SYSTEM and "hover" in fast.FAST_SYSTEM
