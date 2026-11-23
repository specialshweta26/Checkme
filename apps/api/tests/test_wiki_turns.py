"""Live: a wiki agent's "go ahead" ran without its skill, and edit_file on the user's Mac said "File not found"."""

from __future__ import annotations

from types import SimpleNamespace

from app.agent import computer_harness
from app.agent.deep import skill_query

HISTORY = [
    {"role": "user", "content": "ingest https://agilitas.com/pages/our-story into the wiki"},
    {"role": "assistant", "content": "That's outside my role... say go ahead"},
]


def test_go_ahead_routes_skills_on_the_previous_request():
    assert skill_query("go ahead", HISTORY) == HISTORY[0]["content"]
    assert skill_query("Yes, do it", HISTORY) == "Yes, do it"  # not a bare go-ahead: left alone
    assert skill_query("ok", HISTORY) == HISTORY[0]["content"]
    assert skill_query("go ahead", []) == "go ahead"
    assert skill_query("lint the wiki", HISTORY) == "lint the wiki"


def _computer(files: dict, calls: list):
    async def run(ctx, tool, args=None, **kw):
        calls.append((tool, dict(args or {})))
        if tool == "read_file":
            text = files.get(args["path"])
            if text is None:
                return {"ok": False, "error": "not found"}
            return {"ok": True, "path": args["path"], "bytes": len(text.encode()), "content": text, "ran_on": "local_computer"}
        if tool == "write_file":
            files[args["path"]] = args["content"]
            return {"ok": True, "path": args["path"], "verified": True, "ran_on": "local_computer"}
        raise AssertionError(tool)

    return run


async def test_edit_file_changes_the_file_on_the_computer(monkeypatch):
    files = {"~/Desktop/LLM Wiki/wiki/one8.md": "See also [x](wiki/attention-is-all-you-need.md)\n"}
    calls: list = []
    monkeypatch.setattr(computer_harness, "run_computer", _computer(files, calls))
    out = await computer_harness.edit_file(SimpleNamespace(), "~/Desktop/LLM Wiki/wiki/one8.md",
                                           "(wiki/attention-is-all-you-need.md)", "(./attention-is-all-you-need.md)")  # fmt: skip
    assert out["ok"] is True and out["replacements"] == 1
    assert files["~/Desktop/LLM Wiki/wiki/one8.md"] == "See also [x](./attention-is-all-you-need.md)\n"
    assert calls[-1] == ("write_file", {"path": "~/Desktop/LLM Wiki/wiki/one8.md", "content": files["~/Desktop/LLM Wiki/wiki/one8.md"], "overwrite": True})


async def test_edit_file_writes_nothing_when_the_text_is_missing_ambiguous_or_partial(monkeypatch):
    files = {"a.md": "x x", "big.md": "start"}
    calls: list = []
    run = _computer(files, calls)
    monkeypatch.setattr(computer_harness, "run_computer", run)
    assert (await computer_harness.edit_file(None, "a.md", "y", "z"))["ok"] is False
    assert "2 times" in (await computer_harness.edit_file(None, "a.md", "x", "z"))["error"]
    out = await computer_harness.edit_file(None, "a.md", "x", "z", replace_all=True)
    assert out["ok"] and out["replacements"] == 2 and files["a.md"] == "z z"

    async def clipped(ctx, tool, args=None, **kw):
        calls.append((tool, dict(args or {})))
        return {"ok": True, "bytes": 999999, "content": "start\n…truncated"}

    monkeypatch.setattr(computer_harness, "run_computer", clipped)
    before = len([c for c in calls if c[0] == "write_file"])
    assert (await computer_harness.edit_file(None, "big.md", "start", "end"))["ok"] is False
    assert len([c for c in calls if c[0] == "write_file"]) == before


async def test_edit_file_passes_on_a_folder_grant_question(monkeypatch):
    async def need(ctx, tool, args=None, **kw):
        return {"ok": False, "need": "folder_grant", "path": "/elsewhere"}

    monkeypatch.setattr(computer_harness, "run_computer", need)
    assert (await computer_harness.edit_file(None, "/elsewhere/a.md", "a", "b"))["need"] == "folder_grant"


async def test_write_file_overwrites_only_when_asked(monkeypatch):
    calls: list = []
    monkeypatch.setattr(computer_harness, "run_computer", _computer({}, calls))
    await computer_harness.write_file(None, "n.log", "a")
    await computer_harness.write_file(None, "n.log", "b", overwrite=True)
    assert "overwrite" not in calls[0][1] and calls[1][1]["overwrite"] is True
