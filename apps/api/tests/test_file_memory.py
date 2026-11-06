"""File paths tools returned are remembered for later messages."""

from __future__ import annotations

from types import SimpleNamespace

from app.agent import file_memory as fm


def test_paths_from_each_kind_of_result():
    desktop = {"ok": True, "path": "/Users/g/Desktop/organic_farming", "entries": [
        {"name": "organic_farming.pptx", "path": "/Users/g/Desktop/organic_farming/organic_farming.pptx", "type": "file"}]}
    assert fm.paths_in(desktop) == ["/Users/g/Desktop/organic_farming", "/Users/g/Desktop/organic_farming/organic_farming.pptx"]
    cloud = {"path": "/home/user/work", "entries": ["a.txt", "b.csv"]}
    assert fm.paths_in(cloud) == ["/home/user/work", "/home/user/work/a.txt", "/home/user/work/b.csv"]
    search = {"ok": True, "matches": [{"path": "~/Downloads/resume.docx"}, {"path": "~/Desktop/cv.pdf"}]}
    assert fm.paths_in(search) == ["~/Downloads/resume.docx", "~/Desktop/cv.pdf"]
    assert fm.paths_in({"ok": False, "error": "no such folder"}) == []
    assert fm.paths_in("not a result") == []
    assert fm.paths_in({"path": "multi\nline/text"}) == []


def test_merge_keeps_the_latest_once_with_a_cap():
    assert fm.merge(["a/1", "a/2"], ["a/1", "a/3"]) == ["a/2", "a/1", "a/3"]
    assert fm.merge(None, None) == []
    assert fm.merge([f"a/{i}" for i in range(50)], [], keep=5) == [f"a/{i}" for i in range(45, 50)]


def test_remember_adds_to_the_turn():
    ctx = SimpleNamespace(files_seen=[])
    fm.remember(ctx, {"path": "~/Desktop/x.txt"})
    fm.remember(ctx, {"path": "~/Desktop/y.txt"})
    assert ctx.files_seen == ["~/Desktop/x.txt", "~/Desktop/y.txt"]


def test_the_prompt_lists_paths_only_when_there_are_some():
    assert fm.prompt_block([]) == "" and fm.prompt_block(None) == ""
    block = fm.prompt_block(["~/Desktop/a.pptx"])
    assert "~/Desktop/a.pptx" in block and "instead of searching again" in block


async def test_a_file_tool_records_paths_without_changing_its_result():
    from app.agent import harness_extra

    ctx = SimpleNamespace(files_seen=[])
    result = {"path": "~/Desktop/a.txt", "content": "hi"}
    assert harness_extra._seen(ctx, result) is result
    assert ctx.files_seen == ["~/Desktop/a.txt"]
    assert harness_extra._seen(SimpleNamespace(), None) is None  # a missing field never breaks the tool
