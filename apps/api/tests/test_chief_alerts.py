"""Which emails get a card in Chief's window: only mail that needs the user and is about a promise, urgent, or from a
key contact; never Cc-only key-contact mail, the user's own mail, or mail that needs nothing."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.inbox.alerts import alert_reason, mark_alert
from app.inbox.memory_context import MailContext


def _item(cls="reply", role="to"):
    return SimpleNamespace(classification=cls, recipient_role=role, alert_reason="", alert_state="", alerted_at=None)


KEY = MailContext(key_person=True, people=["Priya Nair: Head of Ops. You emailed them 12×; last contact 2 days ago."])
PROMISED = MailContext(key_person=True, people=["Priya Nair: Head of Ops. Open: send the revised SOW (due Friday)."])


@pytest.mark.parametrize(
    "item, data, ctx, reason",
    [
        (_item(), {"about_promise": True}, PROMISED, "promise"),
        (_item(), {"about_promise": True}, KEY, "key_contact"),  # no open promise in memory: the model can't claim one
        (_item(), {"urgent": "true"}, MailContext(), "urgent"),
        (_item("respond"), {}, KEY, "key_contact"),
        (_item(role="cc"), {}, KEY, ""),  # Cc'd FYI from a key contact: no card
        (_item(role="cc"), {"urgent": True}, KEY, "urgent"),  # unless it's urgent
        (_item("discard"), {"urgent": True}, KEY, ""),  # needs nothing from the user
        (_item(), {}, MailContext(), ""),  # a stranger, not urgent
        (_item(), {"urgent": True}, MailContext(from_self=True), ""),  # the user's own address
    ],
)
def test_which_mail_gets_a_card(item, data, ctx, reason):
    assert alert_reason(item, data, ctx) == reason


def test_a_dismissed_card_stays_dismissed_after_a_redraft():
    item = _item()
    mark_alert(item, {"urgent": True}, MailContext())
    assert (item.alert_state, item.alert_reason) == ("new", "urgent") and item.alerted_at is not None
    item.alert_state = "dismissed"
    mark_alert(item, {"urgent": True}, MailContext())
    assert item.alert_state == "dismissed"
    fresh = _item()
    mark_alert(fresh, {"urgent": True}, MailContext())
    mark_alert(fresh, {}, MailContext())  # redrafted: no longer needs the user
    assert fresh.alert_state == ""
