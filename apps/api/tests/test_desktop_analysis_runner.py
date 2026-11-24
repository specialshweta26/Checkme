"""The desktop's read-only analysis runner (apps/desktop/src-tauri/src/analysis.py): data-analysis Python runs without
the permission prompt only when it can't reach the system, and it can read only files under ~/lumio_data."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[2] / "desktop" / "src-tauri" / "src" / "analysis.py"


def _runner():
    spec = importlib.util.spec_from_file_location("lumio_analysis_runner", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ANALYSIS = """
import glob, json, collections
from decimal import Decimal
rows = [r for f in sorted(glob.glob('/x/lumio_data/t/part_*.json')) for r in json.load(open(f))]
g = collections.defaultdict(lambda: [0, Decimal('0')])
for r in rows:
    g[r['channel']][0] += r['qty']
def top(d, n=5):
    return sorted(d.items(), key=lambda kv: kv[1][1], reverse=True)[:n]
print(top(g))
"""


def test_plain_analysis_passes():
    assert _runner().problem(ANALYSIS) == ""


@pytest.mark.parametrize(
    "code,why",
    [
        ("import os", "imports os"),
        ("import subprocess", "imports subprocess"),
        ("from pathlib import Path", "imports from pathlib"),
        ("import pandas", "imports pandas"),
        ("import glob\nglob.os.system('x')", ".system"),
        ("import glob\nm = glob.os", ".os"),
        ("import json\njson.codecs.open('/tmp/x', 'w')", ".open"),
        ("import json\nc = json.codecs", ".codecs"),
        ("import datetime\ndatetime.sys", ".sys"),
        ("import operator", "imports operator"),
        ("import typing", "imports typing"),
        ("eval('1')", "eval"),
        ("getattr(1, 'real')", "getattr"),
        ("x = ().__class__", ".__class__"),
        ("__import__('os')", "__import__"),
        ("class A:\n    pass", "defines a class"),
        ("def __del__(self):\n    pass", "defines __del__"),
        ("type(1)", "type"),
    ],
)
def test_anything_that_can_reach_the_system_is_refused(code, why):
    assert why in _runner().problem(code)


def test_files_are_read_only_and_only_under_lumio_data(tmp_path, monkeypatch):
    mod = _runner()
    monkeypatch.setenv("HOME", str(tmp_path))
    data = tmp_path / "lumio_data" / "t"
    data.mkdir(parents=True)
    (data / "part_0001.json").write_text("[1]")
    secret = tmp_path / ".ssh"
    secret.mkdir()
    (secret / "id").write_text("key")
    real_open = __builtins__["open"] if isinstance(__builtins__, dict) else open
    import builtins
    import glob as glob_module

    saved = (builtins.open, glob_module.glob, glob_module.iglob)
    try:
        mod.confine()
        assert builtins.open(data / "part_0001.json").read() == "[1]"
        with pytest.raises(PermissionError):
            builtins.open(secret / "id")
        with pytest.raises(PermissionError):
            builtins.open(data / "new.txt", "w")
        assert glob_module.glob(str(tmp_path / "*")) == [str(tmp_path / "lumio_data")]
    finally:
        builtins.open, glob_module.glob, glob_module.iglob = saved
    assert real_open is not None and os.path.exists(secret / "id")


@pytest.mark.parametrize("code", ["open('x', 'w')", "open('x', mode='a')", "m = 'r'\nopen('x', m)", "open('x', 'r+')"])
def test_code_that_opens_a_file_to_write_gets_the_prompt(code):
    assert "opens a file to write" in _runner().problem(code)


def test_reading_a_file_is_still_analysis():
    assert _runner().problem("open('/x/lumio_data/a.json')") == ""
    assert _runner().problem("open('/x/lumio_data/a.json', 'r', encoding='utf-8')") == ""
