from connector_core.mail_tools import MAIL_CALENDAR_TOOLS, canonical_mail_folder, folder_from_query
from gmail_mcp.connector import to_gmail_query
from outlook_mcp.connector import to_kql


def test_gmail_dates():
    assert to_gmail_query("invoice after:2026-07-01") == "invoice after:2026/07/01"


def test_kql_dates():
    assert "received>=2026-07-01" in to_kql("invoice after:2026-07-01")


def test_folder_aliases():
    assert canonical_mail_folder("junk") == "spam"
    assert canonical_mail_folder("in:drafts") == "drafts"
    assert folder_from_query("label:spam") == "spam"
    assert folder_from_query("from:ada invoice") is None
    names = {s.name for s in MAIL_CALENDAR_TOOLS}
    assert "empty_folder" in names
    spec = next(s for s in MAIL_CALENDAR_TOOLS if s.name == "empty_folder")
    assert spec.destructive
