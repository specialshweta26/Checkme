"""A recipient sent as one string reached Outlook as one-letter addresses ("couldn't resolve the recipient") and Gmail
as a broken To header. Both connectors now read a list, a comma- or semicolon-separated string, or "Name <a@b>"."""

from __future__ import annotations

from connector_core.text import address_list


def test_every_shape_becomes_a_list_of_addresses():
    assert address_list("friend@example.com") == ["friend@example.com"]
    assert address_list("a@x.com, b@y.org; Name <c@z.io>") == ["a@x.com", "b@y.org", "c@z.io"]
    assert address_list(["a@x.com", {"email": "b@y.org"}, {"emailAddress": {"address": "c@z.io"}}]) == ["a@x.com", "b@y.org", "c@z.io"]
    assert address_list(["a@x.com", "A@x.com"]) == ["a@x.com"]
    assert address_list(None) == [] and address_list("") == [] and address_list("not an address") == []


def test_outlook_and_gmail_build_real_recipients_from_a_string():
    from gmail_mcp.connector import GmailConnector
    from outlook_mcp.connector import _recips

    assert _recips("friend@example.com") == [{"emailAddress": {"address": "friend@example.com"}}]
    msg = GmailConnector._compose("friend@example.com", "Hello", "Hello!", cc="x@y.com")
    assert msg["To"] == "friend@example.com" and msg["Cc"] == "x@y.com"
