"""Saving mail attachments writes file bytes for Gmail and Outlook, and pages past 50."""

from types import SimpleNamespace

from connector_core import BinaryResult

from app.agent.mail_files import save_mail_attachments


class _Mail:
    def __init__(self, slug, pages):
        self.slug = slug
        self.provider = slug
        self.kind = "builtin"
        self.name = slug
        self.pages = pages
        self.calls = []

    async def call(self, name, args):
        self.calls.append((name, dict(args)))
        if name == "search_messages":
            token = args.get("page_token") or ""
            return self.pages.get(token) or {"messages": [], "next_page_token": ""}
        if name == "list_attachments":
            return {"attachments": [{"attachment_id": "a1", "filename": "report.pdf"}]}
        if name == "download_attachment":
            return BinaryResult(filename="report.pdf", content_type="application/pdf", data=b"%PDF")
        raise AssertionError(name)


def _ctx(*connectors):
    return SimpleNamespace(connectors={c.slug: c for c in connectors})


async def test_saves_gmail_and_outlook_into_the_folder(monkeypatch):
    gmail = _Mail(
        "gmail",
        {
            "": {"messages": [{"message_id": "g1", "subject": "One"}], "next_page_token": "p2"},
            "p2": {"messages": [{"message_id": "g2", "subject": "Two"}], "next_page_token": ""},
        },
    )
    outlook = _Mail(
        "outlook",
        {
            "": {
                "messages": [
                    {"message_id": "o1", "subject": "File", "has_attachments": True},
                    {"message_id": "o2", "subject": "No file", "has_attachments": False},
                ],
                "next_page_token": "",
            }
        },
    )
    written = []

    async def write_bytes(_ctx, path, data):
        written.append((path, data))
        return {"ok": True, "path": path, "verified": True}

    out = await save_mail_attachments(_ctx(gmail, outlook), "from:gauri report", "~/Desktop/Reports", write_bytes=write_bytes)
    assert out["saved_count"] == 3
    assert {row["account"] for row in out["saved"]} == {"gmail", "outlook"}
    assert all(path.startswith("~/Desktop/Reports/") for path, _ in written)
    assert len({path for path, _ in written}) == 3
    assert gmail.calls[0][1]["query"].endswith("has:attachment")
    assert outlook.calls[0][1]["query"] == "from:gauri report"
    assert not any(args.get("message_id") == "o2" for name, args in outlook.calls if name == "list_attachments")
    assert out["more"] == {}


async def test_named_account_skips_the_other_mailbox():
    gmail = _Mail("gmail", {"": {"messages": [{"message_id": "g1"}], "next_page_token": ""}})
    outlook = _Mail("outlook", {"": {"messages": [{"message_id": "o1", "has_attachments": True}], "next_page_token": ""}})

    async def write_bytes(_ctx, path, data):
        return {"ok": True, "path": path}

    out = await save_mail_attachments(
        _ctx(gmail, outlook), "invoice", "~/Desktop/Reports", account="outlook", write_bytes=write_bytes
    )
    assert out["saved_count"] == 1
    assert out["saved"][0]["account"] == "outlook"
    assert gmail.calls == []


async def test_page_cap_returns_the_next_token():
    gmail = _Mail("gmail", {"": {"messages": [{"message_id": "g1"}], "next_page_token": "next"}})

    async def write_bytes(_ctx, path, data):
        return {"ok": True, "path": path}

    out = await save_mail_attachments(_ctx(gmail), "invoice", "~/Desktop/Reports", max_messages=1, write_bytes=write_bytes)
    assert out["more"]["gmail"] == "next"
    assert out["scanned"]["gmail"] == 1
