"""Uploads stay usable after the first message, spreadsheets are read, and the cloud computer reads files safely."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.agent import uploads
from app.storage import extract_text
from tests.xlsx_sample import xlsx

SHOES = xlsx([["Name", "Price", "Rating"], ["Runner X", "7499", "4.3"], ["Trail Pro", "6999", "4.1"]])


def test_spreadsheet_uploads_are_read_row_by_row():
    text = extract_text("shoes_under_8000_live 3.xlsx", "", SHOES)
    assert text.splitlines() == ["Sheet: Shoes", "Name\tPrice\tRating", "Runner X\t7499\t4.3", "Trail Pro\t6999\t4.1"]


def test_a_huge_sheet_is_cut_off():
    text = extract_text("big.xlsx", "", xlsx([["row", str(i)] for i in range(900)]), limit=10**6)
    assert "(more rows not shown)" in text and text.count("\n") < 510


def _ctx():
    return SimpleNamespace(user=SimpleNamespace(id=uuid.uuid4()))


async def test_read_upload_returns_the_text(monkeypatch):
    ctx = _ctx()

    async def load(key):
        return SHOES

    monkeypatch.setattr(uploads, "_load", load)
    out = await uploads.read_upload(ctx, f"{ctx.user.id}/abc/shoes.xlsx")
    assert out["ok"] and "Runner X\t7499" in out["content"]


@pytest.mark.parametrize("key", ["someone-else/abc/shoes.xlsx", "", "{me}/../other/x.xlsx"])
async def test_only_this_users_uploads_can_be_used(monkeypatch, key):
    ctx = _ctx()

    async def load(k):
        raise AssertionError("must not load")

    monkeypatch.setattr(uploads, "_load", load)
    key = key.replace("{me}", str(ctx.user.id))
    assert (await uploads.read_upload(ctx, key))["ok"] is False
    assert (await uploads.save_upload_to_computer(ctx, key, "~/Downloads/x.xlsx"))["ok"] is False


async def test_save_upload_writes_the_exact_bytes_to_the_computer(monkeypatch):
    from app.agent import computer_harness

    ctx = _ctx()
    written = {}

    async def load(key):
        return SHOES

    async def write_bytes(c, path, data):
        written.update(path=path, data=data)
        return {"ok": True, "verified": True}

    monkeypatch.setattr(uploads, "_load", load)
    monkeypatch.setattr(computer_harness, "write_bytes", write_bytes)
    out = await uploads.save_upload_to_computer(ctx, f"{ctx.user.id}/abc/shoes.xlsx", "~/Downloads/")
    assert out["ok"] and written == {"path": "~/Downloads/shoes.xlsx", "data": SHOES}
    assert out["path"] == "~/Downloads/shoes.xlsx"


async def test_an_unreadable_upload_says_to_copy_it():
    ctx = _ctx()

    async def load(key):
        return b"\x00\x01binary"

    uploads._load, real = load, uploads._load
    try:
        out = await uploads.read_upload(ctx, f"{ctx.user.id}/abc/archive.zip")
    finally:
        uploads._load = real
    assert out["ok"] is False and "save_upload_to_computer" in out["error"]


@pytest.mark.parametrize(("name", "data", "expect"), [
    ("/home/u/shoes.xlsx", SHOES, "Runner X"),
    ("/home/u/notes.txt", b"plain words", "plain words"),
])
def test_the_cloud_computer_reads_documents_and_text(name, data, expect):
    from app.connectors.daytona_box import _readable

    assert expect in _readable(name, data)["content"]


def test_the_cloud_computer_refuses_binary_instead_of_returning_garbage():
    from app.connectors.daytona_box import _readable

    out = _readable("/home/u/a.zip", b"PK\x03\x04\x00\x00binary")
    assert out["ok"] is False and "binary" in out["error"] and "content" not in out


def test_cloud_file_commands_quote_their_paths():
    import inspect

    from app.connectors import daytona_box

    src = inspect.getsource(daytona_box)
    assert 'f"cat {path!s}"' not in src and 'f"ls -la {path}"' not in src
    assert "cat {shlex.quote(path)}" in src and "ls -la {shlex.quote(path)}" in src


def test_history_keeps_attached_files_for_later_messages():
    import inspect

    from app.chat import routes

    assert "[Attached: " in inspect.getsource(routes) and "attachment_key=" in inspect.getsource(routes)
