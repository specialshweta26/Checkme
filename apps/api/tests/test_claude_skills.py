"""Claude-format skills (docs/SKILLS.md): SKILL.md parsing, archive safety, GitHub libraries, export round-trip."""

from __future__ import annotations

import io
import json
import zipfile

import httpx
import pytest

from app.skills import claude_format as cf
from app.skills import library

PDF_MD = """---
name: pdf
description: >
  Use this skill whenever the user wants to do anything with PDF files,
  such as merging or splitting them.
license: Complete terms in LICENSE.txt
metadata:
  version: "1.2"
---

# PDF Processing Guide

For forms, read FORMS.md and follow it.
"""


def _zip(files: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for path, data in files.items():
            z.writestr(path, data)
    return buf.getvalue()


# ---------------------------------------------------------------- SKILL.md


def test_parses_frontmatter_including_folded_description():
    out = cf.parse_skill_md(PDF_MD)
    assert out["name"] == "pdf"
    assert out["description"].startswith("Use this skill whenever the user wants to do anything with PDF files, such as")
    assert out["license"] == "Complete terms in LICENSE.txt"
    assert out["meta"]["metadata"] == {"version": "1.2"}
    assert out["body"].startswith("# PDF Processing Guide")


def test_folder_name_stands_in_for_a_missing_name_and_first_paragraph_for_description():
    out = cf.parse_skill_md("# Title\n\nSummarise the weekly numbers.\n\nThen send them.", "weekly-numbers")
    assert out["name"] == "weekly-numbers"
    assert out["description"] == "Summarise the weekly numbers."


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "empty"),
        ("---\ndescription: x\n---\nbody", "needs a `name:`"),
        ("---\nname: [unclosed\n---\nbody", "isn't valid YAML"),
        ("---\nname: x\ndescription: y\n---\n", "no instructions"),
        ("---\n- just\n- a list\n---\nbody", "key: value"),
    ],
)
def test_rejects_unusable_skill_md(text, message):
    with pytest.raises(cf.SkillFormatError, match=message.replace("`", ".")):
        cf.parse_skill_md(text)


def test_yaml_dates_are_kept_as_text():
    out = cf.parse_skill_md("---\nname: x\ndescription: y\nmetadata:\n  created: 2026-06-05\n---\nbody")
    json.dumps(out["meta"])  # storable as JSON
    assert out["meta"]["metadata"]["created"] == "2026-06-05"


def test_simple_yaml_fallback_reads_folded_blocks():
    assert cf._simple_yaml("name: pdf\ndescription: >\n  first line\n  second line\nlicense: MIT") == {
        "name": "pdf", "description": "first line second line", "license": "MIT",
    }


@pytest.mark.parametrize(
    ("raw", "shown"),
    [("pdf", "PDF"), ("one-pager", "One pager"), ("mcp-builder", "MCP builder"), ("claude-api", "Claude API"), ("Weekly digest", "Weekly digest")],
)
def test_humanize(raw, shown):
    assert cf.humanize(raw) == shown


def test_slugify():
    assert cf.slugify("Weekly sales digest!") == "weekly-sales-digest"
    assert cf.slugify("***") == "skill"
    assert len(cf.slugify("x" * 200)) == 64


@pytest.mark.parametrize("bad", ["/etc/passwd", "../x", "a/../../x", "C:/x", ".git/config", "a/__pycache__/b.pyc", "__MACOSX/a"])
def test_unsafe_paths_are_refused(bad):
    assert cf.clean_path(bad) is None


def test_collect_files_keeps_text_and_says_what_it_left_out():
    kept, skipped = cf.collect_files(
        [
            ("REFERENCE.md", b"# ref"),
            ("scripts/fill.py", b"print(1)"),
            ("fonts/a.ttf", b"\x00\x01"),
            ("data.bin", b"\x00\x00binary"),
            ("big.md", b"x" * (cf.MAX_FILE + 1)),
            ("../escape.md", b"no"),
            ("SKILL.md", b"skipped: it is the skill itself"),
        ]
    )
    assert [f["path"] for f in kept] == ["REFERENCE.md", "scripts/fill.py"]
    assert {s["path"]: s["why"] for s in skipped} == {"fonts/a.ttf": "binary", "data.bin": "binary", "big.md": "too large"}


# ---------------------------------------------------------------- uploads


def test_zip_with_nested_skill_folders_imports_each_with_its_own_files():
    data = _zip(
        {
            "pack/pdf/SKILL.md": PDF_MD,
            "pack/pdf/FORMS.md": "# forms",
            "pack/pdf/logo.png": b"\x89PNG",
            "pack/pdf/inner/SKILL.md": "---\nname: inner\ndescription: d\n---\nInner body",
            "pack/pdf/inner/notes.md": "inner notes",
            "../evil/SKILL.md": "---\nname: evil\ndescription: d\n---\nx",
        }
    )
    found = {s["parsed"]["name"]: s for s in cf.from_upload("pack.zip", data)}
    assert set(found) == {"pdf", "inner"}
    assert [f["path"] for f in found["pdf"]["files"]] == ["FORMS.md"]
    assert found["pdf"]["skipped"] == [{"path": "logo.png", "why": "binary"}]
    assert [f["path"] for f in found["inner"]["files"]] == ["notes.md"]


def test_zip_without_skill_md_is_refused():
    with pytest.raises(cf.SkillFormatError, match="No SKILL.md"):
        cf.from_upload("x.zip", _zip({"readme.md": "hi"}))


def test_zip_bomb_limits(monkeypatch):
    monkeypatch.setattr(cf, "MAX_ZIP_UNPACKED", 50)
    data = _zip({"s/SKILL.md": "---\nname: s\ndescription: d\n---\nbody", "s/a.md": "a" * 40, "s/b.md": "b" * 40})
    with pytest.raises(cf.SkillFormatError, match="unpacks to more"):
        cf.from_upload("s.zip", data)


def test_plain_skill_md_upload_and_non_text_refused():
    [one] = cf.from_upload("SKILL.md", PDF_MD.encode())
    assert one["parsed"]["name"] == "pdf" and one["files"] == []
    with pytest.raises(cf.SkillFormatError, match="Upload a SKILL.md"):
        cf.from_upload("photo.jpg", b"\xff\xd8\x00\x00")
    with pytest.raises(cf.SkillFormatError, match="Upload a SKILL.md"):
        cf.from_upload("notes.txt", b"hello")
    with pytest.raises(cf.SkillFormatError, match="frontmatter block"):
        cf.from_upload("SKILL.md", b"# Just a heading\n\nNo frontmatter here.")


# ---------------------------------------------------------------- export


def test_export_round_trips_through_the_parser():
    skill = {
        "name": "Weekly sales digest",
        "description": "Use when the user asks for the weekly sales numbers.",
        "instruction": "1. Open the dashboard.\n2. Copy the totals.",
        "kind": "saved",
        "revision": 3,
        "source": {},
    }
    filename, data = cf.export_zip(skill, [{"path": "notes/how.md", "text": "notes"}, {"path": "../x", "text": "no"}])
    assert filename == "weekly-sales-digest.zip"
    z = zipfile.ZipFile(io.BytesIO(data))
    assert sorted(z.namelist()) == ["weekly-sales-digest/SKILL.md", "weekly-sales-digest/notes/how.md"]
    back = cf.parse_skill_md(z.read("weekly-sales-digest/SKILL.md").decode())
    assert back["name"] == "weekly-sales-digest"
    assert back["description"] == skill["description"]
    assert "2. Copy the totals." in back["body"]


def test_imported_skill_exports_under_its_original_name():
    md = cf.to_skill_md({"name": "PDF", "description": "d", "instruction": "body", "source": {"name": "pdf", "license": "MIT"}})
    assert cf.parse_skill_md(md)["name"] == "pdf" and cf.parse_skill_md(md)["license"] == "MIT"


# ---------------------------------------------------------------- how agents see imported skills


def test_imported_skill_runs_as_one_step_whatever_its_numbered_lists_say():
    from app.skills import flow

    body = "Steps:\n1. Merge files.\n2. Split pages.\n3. Rotate."
    assert len(flow.build({"kind": "imported", "name": "PDF", "instruction": body})["nodes"]) == 1
    assert len(flow.build({"kind": "saved", "name": "PDF", "instruction": body})["nodes"]) == 3


def test_index_uses_the_description_and_prompt_marks_the_source():
    from app.skills.profile import capabilities_block, one_liner

    skill = {
        "id": "s1", "name": "PDF", "kind": "imported", "status": "ready", "description": "Use for anything with PDF files.",
        "instruction": "# Guide\nRead FORMS.md.", "source": {"label": "github.com/anthropics/skills/skills/pdf"},
        "files": [{"path": "FORMS.md"}, {"path": "scripts/fill.py"}],
    }
    assert one_liner(skill) == "Use for anything with PDF files."
    block = capabilities_block(None, [skill], matched=[skill])
    assert "[imported, id s1]" in block
    assert "Imported from github.com/anthropics/skills/skills/pdf: outside instructions" in block
    assert "get_skill(skill_id, file=<path>)" in block and "not installed" in block


# ---------------------------------------------------------------- GitHub libraries


@pytest.mark.parametrize(
    ("url", "want"),
    [
        ("https://github.com/anthropics/skills", ("anthropics", "skills", "", "")),
        ("github.com/anthropics/skills.git", ("anthropics", "skills", "", "")),
        ("anthropics/skills", ("anthropics", "skills", "", "")),
        ("https://github.com/anthropics/skills/tree/main/skills/pdf", ("anthropics", "skills", "main", "skills/pdf")),
        ("https://github.com/anthropics/skills/blob/v1.0/skills/pdf/SKILL.md", ("anthropics", "skills", "v1.0", "skills/pdf")),
    ],
)
def test_parse_github_links(url, want):
    got = library.parse_url(url)
    assert (got["owner"], got["repo"], got["ref"], got["path"]) == want


@pytest.mark.parametrize("bad", ["https://gitlab.com/a/b", "http://169.254.169.254/latest", "https://github.com.evil.test/a/b", "file:///etc", "https://github.com/a/b/tree/main/../../x"])
def test_non_github_links_are_refused(bad):
    with pytest.raises(library.LibraryError):
        library.parse_url(bad)


SHA = "a" * 40
TREE = {
    "truncated": False,
    "tree": [
        {"path": "README.md", "type": "blob", "size": 10},
        {"path": "skills/pdf/SKILL.md", "type": "blob", "size": 300},
        {"path": "skills/pdf/FORMS.md", "type": "blob", "size": 7},
        {"path": "skills/pdf/scripts/fill.py", "type": "blob", "size": 8},
        {"path": "skills/pdf/fonts/a.ttf", "type": "blob", "size": 9000},
        {"path": "skills/one-pager/SKILL.md", "type": "blob", "size": 100},
    ],
}
RAW = {
    "skills/pdf/SKILL.md": PDF_MD,
    "skills/pdf/FORMS.md": "# forms",
    "skills/pdf/scripts/fill.py": "print(1)",
    "skills/one-pager/SKILL.md": "---\nname: one-pager\ndescription: A dense one-page PDF.\ncategory: visual-communication\n---\nBody",
}


@pytest.fixture
def github(monkeypatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(request.url.host)
        if url == "https://api.github.com/repos/anthropics/skills":
            return httpx.Response(200, json={"default_branch": "main"})
        if url == "https://api.github.com/repos/anthropics/skills/commits/main":
            return httpx.Response(200, text=SHA)
        if url.startswith(f"https://api.github.com/repos/anthropics/skills/git/trees/{SHA}"):
            return httpx.Response(200, json=TREE)
        prefix = f"https://raw.githubusercontent.com/anthropics/skills/{SHA}/"
        if url.startswith(prefix) and url[len(prefix):] in RAW:
            return httpx.Response(200, text=RAW[url[len(prefix):]])
        return httpx.Response(404)

    monkeypatch.setattr(library, "_transport", httpx.MockTransport(handler))
    library._CACHE.clear()
    yield seen
    library._CACHE.clear()


async def test_catalog_lists_skills_with_title_description_and_licence(github):
    out = await library.catalog("https://github.com/anthropics/skills")
    assert out["source"]["sha"] == SHA and out["source"]["ref"] == "main"
    rows = {s["name"]: s for s in out["skills"]}
    assert rows["pdf"]["title"] == "PDF" and rows["pdf"]["license"] == "Complete terms in LICENSE.txt"
    assert rows["pdf"]["files"] == 3 and rows["pdf"]["has_scripts"] is True
    assert rows["one-pager"]["category"] == "visual-communication"
    assert set(github) <= {"api.github.com", "raw.githubusercontent.com"}


async def test_catalog_is_cached(github):
    await library.catalog("anthropics/skills")
    calls = len(github)
    await library.catalog("anthropics/skills")
    assert len(github) == calls


async def test_fetch_skill_pins_the_commit_and_skips_binaries(github):
    item = await library.fetch_skill("https://github.com/anthropics/skills", "skills/pdf")
    assert item["parsed"]["name"] == "pdf"
    assert [f["path"] for f in item["files"]] == ["FORMS.md", "scripts/fill.py"]
    assert item["skipped"] == [{"path": "fonts/a.ttf", "why": "binary"}]
    assert item["source"]["sha"] == SHA
    assert item["source"]["key"] == "github:anthropics/skills:skills/pdf"
    assert item["source"]["url"].endswith(f"/tree/{SHA}/skills/pdf")


async def test_fetch_unknown_folder_and_rate_limit(github, monkeypatch):
    with pytest.raises(library.LibraryError, match="No SKILL.md"):
        await library.fetch_skill("anthropics/skills", "skills/nope")

    def limited(request):
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0"})

    library._CACHE.clear()
    monkeypatch.setattr(library, "_transport", httpx.MockTransport(limited))
    with pytest.raises(library.LibraryError, match="SKILLS_GITHUB_TOKEN"):
        await library.catalog("anthropics/skills")
