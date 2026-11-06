"""The agent quality checks' own logic, without a model or network."""

from __future__ import annotations

import pytest

from evals.harness import FakeComputer, Result, Scenario, check, failed, summary
from evals.scenarios import scenarios


def test_check_reports_each_kind_of_problem():
    s = Scenario("x", "hi", must_call=["read_file", "list_dir|list_files"], must_not_call=["browser_*"],
                 reply_has=["could not"], reply_lacks=["secret"], max_seconds=5, max_tool_calls=2)
    calls = [("browser_act", {}), ("list_files", {}), ("list_files", {})]
    problems = check(s, calls, "here is the secret", 9)
    assert problems == [
        "didn't call read_file",
        "called browser_act, which it shouldn't",
        "reply lacks /could not/",
        "reply has /secret/",
        "took 9s, limit 5s",
        "3 tool calls, limit 2",
    ]


def test_check_passes_a_good_run_and_reads_curly_apostrophes():
    s = Scenario("x", "hi", must_call=["list_dir|list_files"], reply_has=["couldn't"])
    assert check(s, [("list_dir", {})], "I couldn\u2019t find it", 1) == []


def test_known_issues_are_reported_but_do_not_fail_the_run():
    results = [Result("a", True), Result("b", False, "searches again", problems=["called search_files"])]
    assert not failed(results)
    assert "KNOWN" in summary(results) and "1 known issue" in summary(results)
    assert failed([Result("c", False, problems=["x"])])


@pytest.mark.parametrize(("path", "norm"), [("~/Desktop/a.txt", "~/Desktop/a.txt"), ("/Users/someone/Desktop/a.txt", "~/Desktop/a.txt"),
                                            ("Desktop/a.txt", "~/Desktop/a.txt"), ("~/Desktop/", "~/Desktop")])
def test_paths_are_normalised(path, norm):
    assert FakeComputer._norm(path) == norm


async def test_fake_computer_lists_reads_and_refuses_the_rest():
    pc = FakeComputer({"~/Desktop/a/b.txt": "hello", "~/Desktop/c.bin": b"\x00"})
    listed = await pc.run(None, "list_dir", {"path": "~/Desktop"})
    assert [e["name"] for e in listed["entries"]] == ["a", "c.bin"]
    assert (await pc.run(None, "read_file", {"path": "~/Desktop/a/b.txt"}))["content"] == "hello"
    assert "binary" in (await pc.run(None, "read_file", {"path": "~/Desktop/c.bin"}))["error"]
    assert (await pc.run(None, "list_dir", {"path": "~/Nope"}))["ok"] is False
    assert (await pc.run(None, "browser_act", {}))["ok"] is False
    assert [c for c, _ in pc.calls] == ["list_dir", "read_file", "read_file", "list_dir", "browser_act"]


def test_scenarios_have_unique_names_and_a_check():
    names = [s.name for s in scenarios()]
    assert len(names) == len(set(names))
    for s in scenarios():
        assert s.must_call or s.must_not_call or s.reply_has, s.name
