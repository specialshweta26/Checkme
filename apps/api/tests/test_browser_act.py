"""Generic type-ahead / dropdown picking and browser-operator actions."""

from pathlib import Path

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
