from types import SimpleNamespace

from app.inbox.service import KEEP_PENDING, _address_list, _badge_payload, apply_triage, newest_first, normalize_class, parse_json_object, received_sort_key, serialize, split_kept
from connector_core.mail_tools import merge_message_lists, recipient_role


def test_normalize_class_aliases():
    assert normalize_class("REPLY") == "reply"
    assert normalize_class("needs reply") == "reply"
    assert normalize_class("follow-up") == "respond"
    assert normalize_class("normal") == "discard"
    assert normalize_class("spam") == ""


def test_parse_json_object_tolerates_prose():
    data = parse_json_object('Here you go\n{"classification":"discard","draft":"No reply needed.","summary":"FYI"}')
    assert data["classification"] == "discard"
    assert data["draft"].startswith("No reply")


def test_apply_triage_fills_discard_draft():
    item = SimpleNamespace(classification="", summary="", key_points=[], draft="")
    apply_triage(
        item,
        {
            "classification": "ignore",
            "summary": "Quarantine notice",
            "key_points": ["Open the portal", "Release only if expected"],
            "draft": "",
        },
    )
    assert item.classification == "discard"
    assert item.draft == "No reply is needed to this message."
    assert len(item.key_points) == 2


def test_serialize_shape():
    item = SimpleNamespace(
        id="abc",
        message_id="m1",
        thread_id="t1",
        subject="Hello",
        sender="Ada <ada@example.com>",
        received_at="20/10/2026, 03:00:00",
        snippet="Hi",
        classification="reply",
        summary="Ada asked for the deck.",
        key_points=["Send the deck"],
        draft="Hi Ada, attaching the deck.",
        status="pending",
        unread=True,
        seen=False,
    )
    out = serialize(item, "Gmail", "gmail")
    assert out["from"] == "Ada <ada@example.com>"
    assert out["classification"] == "reply"
    assert out["connector"] == "Gmail"
    assert out["connector_slug"] == "gmail"
    assert out["recipient_role"] == "to"
    assert out["to"] == ""
    assert out["cc"] == ""


def test_received_sort_key_newest_first():
    older = received_sort_key("30 Jun 2026, 12:29 PM IST")
    newer = received_sort_key("19 Oct 2026, 3:11 PM IST")
    assert newer > older
    rows = newest_first(
        [
            SimpleNamespace(received_at="30 Jun 2026, 12:29 PM IST", created_at=None, subject="old"),
            SimpleNamespace(received_at="19 Oct 2026, 3:11 PM IST", created_at=None, subject="new"),
        ]
    )
    assert [r.subject for r in rows] == ["new", "old"]


def test_split_kept_drops_oldest_past_limit():
    rows = [
        SimpleNamespace(received_at="01 Oct 2026, 9:00 AM IST", created_at=None, subject=f"m{i}")
        for i in range(KEEP_PENDING + 5)
    ]
    rows.append(SimpleNamespace(received_at="21 Oct 2026, 12:00 PM IST", created_at=None, subject="newest"))
    keep, drop = split_kept(rows)
    assert len(keep) == KEEP_PENDING
    assert len(drop) == 6
    assert keep[0].subject == "newest"
    assert "newest" not in {r.subject for r in drop}


def test_badge_counts_only_unseen():
    items = [
        SimpleNamespace(id="1", subject="New", sender="Ada", received_at="", classification="reply", unread=True, seen=False),
        SimpleNamespace(id="2", subject="Seen", sender="Ada", received_at="", classification="respond", unread=True, seen=True),
    ]
    out = _badge_payload(items)
    assert out["count"] == 1
    assert out["pending"] == 2
    assert [row["subject"] for row in out["items"]] == ["New"]
    marked = _badge_payload(
        [
            SimpleNamespace(id="1", subject="New", sender="Ada", received_at="", classification="reply", unread=True, seen=True),
            SimpleNamespace(id="2", subject="Seen", sender="Ada", received_at="", classification="respond", unread=True, seen=True),
        ],
        alerts=items[:1],
    )
    assert marked["count"] == 0
    assert marked["pending"] == 2
    assert [row["subject"] for row in marked["items"]] == ["New"]
    assert out["items"][0]["recipient_role"] == "to"


def test_recipient_role_to_cc_bcc():
    me = "shweta@agilitas.com"
    assert recipient_role(me, "Shweta <shweta@agilitas.com>", "Ada <ada@example.com>") == "to"
    assert recipient_role(me, "Ada <ada@example.com>", "Shweta <shweta@agilitas.com>") == "cc"
    assert recipient_role(me, "Ada <ada@example.com>", "Pat <pat@example.com>") == "bcc"
    assert recipient_role("", "Ada <ada@example.com>", "Pat <pat@example.com>") == "to"


def test_merge_message_lists_keeps_cc_hits():
    inbox = [{"message_id": f"to-{i}", "subject": f"to {i}"} for i in range(3)]
    copied = [{"message_id": "cc-1", "subject": "cc me", "recipient_role": "cc"}]
    merged = merge_message_lists(inbox, copied)
    assert [row["message_id"] for row in merged] == ["to-0", "to-1", "to-2", "cc-1"]
    again = merge_message_lists(inbox + copied, copied)
    assert [row["message_id"] for row in again] == ["to-0", "to-1", "to-2", "cc-1"]


def test_address_list_parses_cc_bcc():
    assert _address_list("ada@example.com, pat@example.com;  Sam <sam@example.com>") == [
        "ada@example.com",
        "pat@example.com",
        "sam@example.com",
    ]
    assert _address_list(["ada@example.com", "ada@example.com", ""]) == ["ada@example.com"]
