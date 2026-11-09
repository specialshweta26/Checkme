"""Generic type-ahead / dropdown picking and browser-operator actions."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.connectors.computer_daemon import (
    _SNAPSHOT_JS,
    _SUGGEST_JS,
    _act_step,
    _type_into,
    best_option,
    launch_options,
    looks_combobox,
    pick_suggestion,
    tree_line,
    safe_upload_path,
    score_option,
)


def test_score_option_matches_codes_and_labels():
    assert score_option("Bengaluru", "Bengaluru (BLR)") >= 70
    assert score_option("BLR", "Bengaluru Kempegowda Intl. (BLR)") >= 70
    assert score_option("Patna", "Patna, India (PAT)") >= 70
    assert score_option("Bengaluru BLR", "Bengaluru (BLR)") >= 50
    assert score_option("zzz", "Bengaluru (BLR)") == 0
    assert score_option("", "Bengaluru") == 0


def test_best_option_picks_the_closest_row():
    rows = [
        {"name": "Belgaum (IXG)", "x": 10, "y": 10},
        {"name": "Bengaluru (BLR)", "x": 20, "y": 40},
        {"name": "Bhopal (BHO)", "x": 30, "y": 60},
    ]
    hit = best_option("Bengaluru", rows)
    assert hit["name"] == "Bengaluru (BLR)"
    assert best_option("nowhere", rows) is None


def test_looks_combobox_from_role_and_aria():
    assert looks_combobox({"role": "combobox"})
    assert looks_combobox({"role": "searchbox"})
    assert not looks_combobox({"role": "textbox", "tag": "input"})
    assert not looks_combobox({"tag": "select"})
    assert looks_combobox({"role": "textbox"}, {"ac": "list", "popup": "listbox"})
    assert looks_combobox({}, {"ce": True})


class _Keys:
    def __init__(self):
        self.pressed: list[str] = []
        self.typed: list[str] = []

    def press(self, key):
        self.pressed.append(key)

    def type(self, text, delay=0):
        self.typed.append(text)


class _Mouse:
    def __init__(self):
        self.clicks: list[tuple[int, int]] = []

    def click(self, x, y):
        self.clicks.append((int(x), int(y)))


class _Page:
    def __init__(self, options=None):
        self.keyboard = _Keys()
        self.mouse = _Mouse()
        self.frames = [self]
        self.options = list(options or [])
        self.url = "https://example.com/search"

    def wait_for_timeout(self, ms):
        return None

    def evaluate(self, js, *args):
        return self.options


class _Loc:
    def __init__(self, tag="input", role="combobox"):
        self.tag = tag
        self.role = role
        self.selected = None
        self.filled = None
        self.clicks = 0

    def evaluate(self, js):
        return {
            "role": self.role,
            "ac": "list" if self.role == "combobox" else "",
            "popup": "listbox" if self.role == "combobox" else "",
            "list": "",
            "tag": self.tag,
            "ce": False,
        }

    def select_option(self, value, timeout=0):
        if self.tag != "select":
            raise RuntimeError("Element is not a <select> element")
        self.selected = value

    def click(self, timeout=0, button="left"):
        self.clicks += 1
        self.button = button

    def fill(self, text, timeout=0):
        self.filled = text

    def count(self):
        return 1

    def dblclick(self, timeout=0):
        self.dbl = True

    def set_input_files(self, path, timeout=0):
        self.files = path

    def wait_for(self, state="visible", timeout=0):
        self.seen = state

    @property
    def first(self):
        return self


def test_pick_native_select_does_not_type():
    loc = _Loc(tag="select", role="")
    page = _Page()
    how = pick_suggestion(page, loc, {"tag": "select", "role": "combobox"}, "PAT")
    assert how == "native"
    assert loc.selected == "PAT"
    assert page.keyboard.typed == []
    assert page.mouse.clicks == []


def test_pick_clicks_matching_typeahead_option():
    loc = _Loc()
    page = _Page([{"name": "Bengaluru (BLR)", "x": 120, "y": 240}, {"name": "Belgaum (IXG)", "x": 120, "y": 280}])
    how = pick_suggestion(page, loc, {"role": "combobox", "tag": "input", "x": 80, "y": 40}, "Bengaluru")
    assert how == "option"
    assert loc.clicks >= 1
    assert loc.filled is None
    assert page.keyboard.typed == ["Bengaluru"]
    assert page.mouse.clicks == [(120, 240)]


def test_pick_keyboard_fallback_when_list_has_no_match():
    loc = _Loc()
    page = _Page([])
    how = pick_suggestion(page, loc, {"role": "combobox", "tag": "input", "x": 80, "y": 40}, "Patna")
    assert how == "keyboard"
    assert page.keyboard.typed == ["Patna"]
    assert "ArrowDown" in page.keyboard.pressed
    assert "Enter" in page.keyboard.pressed


def test_fill_on_combobox_types_instead_of_setting_value():
    loc = _Loc()
    page = _Page()
    _type_into(page, loc, {"role": "combobox", "tag": "input"}, "Bengaluru", replace=True)
    assert loc.filled is None
    assert loc.clicks >= 1
    assert page.keyboard.typed == ["Bengaluru"]


def test_fill_on_plain_input_uses_fill():
    loc = _Loc(tag="input", role="textbox")
    page = _Page()
    _type_into(page, loc, {"role": "textbox", "tag": "input"}, "hello", replace=True)
    assert loc.filled == "hello"
    assert page.keyboard.typed == []


def test_snapshot_script_covers_overlays_iframes_and_shadow():
    assert "shadowRoot" in _SNAPSHOT_JS
    assert 'role="option"' in _SNAPSHOT_JS
    assert 'role="listbox"' in _SNAPSHOT_JS
    assert "shadowRoot" in _SUGGEST_JS


def test_safe_upload_path_stays_in_workspace():
    allowed = Path("/tmp/checkme-upload-test.txt")
    allowed.write_text("hi", encoding="utf-8")
    try:
        assert safe_upload_path(str(allowed)).name == "checkme-upload-test.txt"
    finally:
        allowed.unlink(missing_ok=True)
    with pytest.raises(RuntimeError, match="outside|not found"):
        safe_upload_path("/etc/hosts")


def test_launch_options_accept_downloads():
    opts = launch_options(True, {"locale": "en-IN"})
    assert opts["accept_downloads"] is True
    assert opts["downloads_path"]


def test_dblclick_and_upload_actions():
    loc = _Loc(tag="input", role="file")
    page = _Page()
    page.locator = lambda *a, **k: loc  # type: ignore[method-assign]
    page.get_by_label = page.locator
    page.get_by_placeholder = page.locator
    page.get_by_role = lambda *a, **k: loc
    page.get_by_text = page.locator
    _act_step(page, {"kind": "dblclick", "ref": "Open"})
    assert loc.dbl is True
    dest = Path("/tmp/checkme-upload-act.txt")
    dest.write_text("file", encoding="utf-8")
    try:
        extra = _act_step(page, {"kind": "upload", "ref": "Open", "path": str(dest)})
        assert extra["upload"]["filename"] == "checkme-upload-act.txt"
        assert loc.files == str(dest.resolve())
    finally:
        dest.unlink(missing_ok=True)


class _SortLoc(_Loc):
    OPTIONS = [{"value": "az", "label": "Name (A to Z)"}, {"value": "lohi", "label": "Price (low to high)"}]

    def evaluate(self, js):
        return self.OPTIONS if "el.options" in js else super().evaluate(js)


def test_native_select_takes_a_label_or_part_of_one():
    for text in ("Price (low to high)", "price (LOW to high)", "low to high", "lohi"):
        loc = _SortLoc(tag="select", role="")
        assert pick_suggestion(_Page(), loc, {"tag": "select"}, text) == "native"
        assert loc.selected == "lohi"


def test_native_select_names_the_choices_when_nothing_matches():
    loc = _SortLoc(tag="select", role="")
    with pytest.raises(RuntimeError, match=r'no option "Cheapest".*Name \(A to Z\) \| Price \(low to high\)'):
        pick_suggestion(_Page(), loc, {"tag": "select"}, "Cheapest")


def test_a_link_is_never_treated_as_a_dropdown():
    page = _Page()
    with pytest.raises(RuntimeError, match="link, not a dropdown"):
        pick_suggestion(page, _Loc(tag="a", role="link"), {"tag": "a", "role": "link", "href": "https://x.example/"}, "Price")
    assert page.keyboard.typed == [] and page.mouse.clicks == []


def test_tree_line_says_what_a_repeated_control_belongs_to():
    line = tree_line({"ref": "e4", "role": "button", "name": "Add to cart", "near": "Sauce Labs Onesie"})
    assert line == "- e4 button Add to cart (Sauce Labs Onesie)"


def test_styled_dropdown_picks_from_the_list_without_waiting_or_typing():
    loc = _Loc(tag="div", role="")
    page = _Page([{"name": "Admin", "x": 50, "y": 90}, {"name": "ESS", "x": 50, "y": 120}])
    assert pick_suggestion(page, loc, {"tag": "div", "x": 50, "y": 40}, "ESS") == "option"
    assert page.mouse.clicks == [(50, 120)] and page.keyboard.typed == [] and loc.selected is None


def test_styled_dropdown_names_what_it_shows_when_nothing_matches():
    page = _Page([{"name": "Admin", "x": 50, "y": 90}])
    with pytest.raises(RuntimeError, match='no option "Manager".*Admin'):
        pick_suggestion(page, _Loc(tag="div", role=""), {"tag": "div", "x": 50, "y": 40}, "Manager")


def test_a_dropdown_already_on_that_option_is_not_a_failure():
    class Loc(_SortLoc):
        def input_value(self, timeout=0):
            return "lohi"

    loc = Loc(tag="select", role="")
    assert pick_suggestion(_Page(), loc, {"tag": "select"}, "Price (low to high)") == "already"
    assert loc.selected is None


def test_failed_request_addresses_lose_their_query_strings():
    from app.connectors.computer_daemon import _strip_url

    assert _strip_url("https://x.example/api/a?token=secret#frag") == "https://x.example/api/a"


def test_a_page_answers_pop_ups_safely_and_reports_them():
    from app.connectors import computer_daemon as d

    handlers = {}
    page = SimpleNamespace(on=lambda name, fn: handlers.__setitem__(name, fn))
    d._watch_page(page)

    class Dialog:
        type = "confirm"
        message = "Delete this?"
        default_value = ""
        answered = None

        def accept(self, text=""):
            self.answered = "accept"

        def dismiss(self):
            self.answered = "dismiss"

    first = Dialog()
    handlers["dialog"](first)
    assert first.answered == "dismiss"
    d._accept_next.update(on=True, text=None)
    second = Dialog()
    handlers["dialog"](second)
    assert second.answered == "accept" and d._accept_next["on"] is False
    out = {}
    d._take_dialogs(out, page)
    assert [row["answer"] for row in out["dialogs"]] == ["declined", "accepted"]
    again = {}
    d._take_dialogs(again, page)
    assert "dialogs" not in again


def test_tree_lines_show_a_controls_state():
    assert tree_line({"ref": "e1", "role": "checkbox", "name": "checkbox 1", "input_type": "checkbox", "checked": False}) == "- e1 checkbox checkbox 1 [checkbox] [not checked]"
    assert "[checked]" in tree_line({"ref": "e2", "role": "checkbox", "name": "b", "checked": True})
    line = tree_line({"ref": "e3", "role": "textbox", "name": "City", "value": "Pune", "disabled": True})
    assert "[disabled]" in line and "[value: Pune]" in line


def test_finish_check_spots_a_reply_that_only_promises_work():
    from app.agent import finish_check as fc

    ctx = SimpleNamespace(teammate_depth=0, pending=[], question=None, used_tools=[])
    assert fc.only_promised(ctx, "I’ve started checking the three pages. I’ll report the text I can read from each.")
    assert not fc.only_promised(ctx, "Paris is the capital of France.")
    assert not fc.only_promised(ctx, "I'll check that. Which account do you mean?")
    assert not fc.only_promised(SimpleNamespace(teammate_depth=0, pending=[], question=None, used_tools=["browser_act"]), "I'll report back.")


def test_text_inside_frames_is_read_and_labelled():
    from app.connectors.computer_daemon import _frame_text

    class Frame:
        def __init__(self, url, text):
            self.url, self.text = url, text

        def evaluate(self, js):
            if self.text is None:
                raise RuntimeError("detached")
            return self.text

    page = SimpleNamespace(frames=[Frame("https://a.example/", "main"), Frame("https://b.example/x?secret=1", "Parent frame"), Frame("about:blank", ""), Frame("https://c.example/", None)])
    out = _frame_text(page)
    assert out == "[frame https://b.example/x]\nParent frame"


def test_a_click_that_opens_a_new_tab_counts_as_working():
    from app.connectors import computer_daemon as d

    opened = []
    page = SimpleNamespace(context=SimpleNamespace(pages=opened), wait_for_timeout=lambda ms: opened.append(object()), evaluate=lambda js: "same")
    assert d._still_same(page, "same", 1.0) is False
    assert len(opened) == 1


def test_a_click_that_raises_a_pop_up_counts_as_working():
    from app.connectors import computer_daemon as d

    page = SimpleNamespace(context=SimpleNamespace(pages=[]), evaluate=lambda js: "same")
    d._page_watch[id(page)] = {"dialogs": [], "errors": [], "failed": []}
    page.wait_for_timeout = lambda ms: d._page_watch[id(page)]["dialogs"].append({"type": "alert"})
    try:
        assert d._still_same(page, "same", 1.0) is False
    finally:
        d._page_watch.pop(id(page), None)
