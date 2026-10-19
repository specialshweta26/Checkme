from gmail_mcp.connector import to_gmail_query
from outlook_mcp.connector import to_kql


def test_gmail_dates():
    assert to_gmail_query("invoice after:2026-07-01") == "invoice after:2026/07/01"


def test_kql_dates():
    assert "received>=2026-07-01" in to_kql("invoice after:2026-07-01")
