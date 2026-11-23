"""A meeting or mail automation the user asked to "send me directly" runs without asking for what it sends to them, and
an existing automation can be changed by its id (app/agent/staff.py update_automation). Before: meeting and mail
triggers always asked, and the only update tool took a scheduled task's UUID, so "send it directly" failed with
"badly formed hexadecimal UUID" and the agent made a second automation instead."""

from __future__ import annotations

from types import SimpleNamespace

from app.agent import deep, staff
from app.models import Automation, User
from tests.test_event_triggers import _jobs, _user, factory, needs_db  # noqa: F401 - the fixture
from app.automations import triggers


async def _make(factory, user_id, **args):
    async with factory() as db:
        user = await db.get(User, user_id)
        return await staff.create_automation(db, user, {"name": "Pre-reads", "trigger": "45", "action": "Send me a pre-read", **args})


async def _update(factory, user_id, args):
    async with factory() as db:
        return await staff.dispatch(db, await db.get(User, user_id), None, "update_automation", args)


@needs_db
async def test_a_meeting_automation_can_send_directly(factory):
    user_id = await _user(factory)
    made = await _make(factory, user_id, trigger_type="meeting", requires_approval=False)
    assert made["automation"]["requires_approval"] is False and "directly" in made["note"]
    asks = await _make(factory, user_id, trigger_type="meeting")
    assert asks["automation"]["requires_approval"] is True  # asking stays the default
    as_text = await _make(factory, user_id, trigger_type="meeting", requires_approval="false")
    assert as_text["automation"]["requires_approval"] is False  # bool("false") used to be True


@needs_db
async def test_page_and_inbox_watches_always_ask(factory):
    async with factory() as db:
        user = await db.get(User, await _user(factory))
        made = await triggers.create_trigger(db, user, trigger_type="inbox", name="Busy", trigger="5", action="Tell me",
                                             requires_approval=False)  # fmt: skip
    assert made["requires_approval"] is True


@needs_db
async def test_an_automation_is_changed_by_its_id(factory):
    user_id = await _user(factory)
    made = await _make(factory, user_id, trigger_type="meeting")
    aid = made["automation"]["id"]
    out = await _update(factory, user_id, {"automation_id": aid, "requires_approval": False, "trigger": "30"})
    assert out["updated"] and out["automation"]["requires_approval"] is False and out["automation"]["trigger"] == "30"
    off = await _update(factory, user_id, {"automation_id": str(aid), "enabled": False})
    assert off["automation"]["enabled"] is False
    # The agent used update_scheduled_task with an automation's id: that now changes the automation.
    async with factory() as db:
        back = await staff.update_scheduled_task(db, await db.get(User, user_id), {"task_id": str(aid), "status": "active"})
    assert back["automation"]["enabled"] is True
    gone = await _update(factory, user_id, {"automation_id": aid, "delete": True})
    async with factory() as db:
        assert gone["deleted"] and await db.get(Automation, aid) is None


@needs_db
async def test_someone_elses_automation_is_not_found(factory):
    owner, other = await _user(factory), await _user(factory)
    made = await _make(factory, owner, trigger_type="meeting")
    out = await _update(factory, other, {"automation_id": made["automation"]["id"], "requires_approval": False})
    assert out == {"error": "automation not found"}
    assert (await _update(factory, owner, {"automation_id": "abc"}))["error"].startswith("automation_id")


@needs_db
async def test_a_direct_send_automation_runs_its_job_without_asking(factory, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from connector_core.calendar import format_local

    user_id = await _user(factory)
    await _make(factory, user_id, trigger_type="meeting", requires_approval=False)
    now = datetime.now(timezone.utc)

    class Calendar:
        async def call(self, name, args):
            return {"events": [{"event_id": "m1", "title": "Review", "start": format_local(now + timedelta(minutes=30))}]}

    async def connectors(db, user):
        return [Calendar()]

    monkeypatch.setattr("app.inbox.service.mail_connectors", connectors)
    assert await triggers.check_meetings(factory, now) == 1
    jobs = await _jobs(factory, user_id)
    assert jobs[0].meta["requires_approval"] is False  # the runner passes auto_confirm for it


def _ctx(**kw):
    return SimpleNamespace(**{"auto_confirm": False, "job": None, "untrusted_seen": False, "injections": [], **kw})


def test_only_the_user_in_chat_can_turn_ask_first_off():
    off = {"automation_id": 1, "requires_approval": False}
    assert deep._approval_off_refused(_ctx(), "update_automation", off) is None
    assert deep._approval_off_refused(_ctx(), "create_automation", {"requires_approval": "false"}) is None
    # After reading mail or a page, or inside a scheduled / event run, the request may have come from that content.
    for ctx in (_ctx(untrusted_seen=True), _ctx(auto_confirm=True), _ctx(job=object()), _ctx(injections=[{}])):
        assert "error" in deep._approval_off_refused(ctx, "update_automation", off)
    # Turning it back on, or other changes, are always fine.
    assert deep._approval_off_refused(_ctx(untrusted_seen=True), "update_automation", {"automation_id": 1, "requires_approval": True}) is None
    assert deep._approval_off_refused(_ctx(untrusted_seen=True), "update_automation", {"automation_id": 1, "enabled": False}) is None


def test_the_users_own_words_turn_ask_first_off_even_after_reading_mail():
    off = {"automation_id": 1, "requires_approval": False}
    for said in ("Send me the pre-read directly", "no approval needed", "don't ask me, just send it", "Approved", "yes"):
        assert deep._approval_off_refused(_ctx(untrusted_seen=True, injections=[{}], query=said), "update_automation", off) is None, said
    assert "error" in deep._approval_off_refused(_ctx(untrusted_seen=True, query="check my calendar"), "update_automation", off)
    assert "error" in deep._approval_off_refused(_ctx(auto_confirm=True, query="send directly"), "update_automation", off)


def test_mail_the_user_asked_for_to_an_address_they_typed_needs_no_card():
    from app.agent.approval import asked_in_chat

    asked = "Send email to friend@example.com saying hello to him"
    assert asked_in_chat("send_message", {"to": "friend@example.com", "subject": "Hello", "body": "Hello!"}, asked)
    assert asked_in_chat("send_message", {"to": ["Friend@Example.com"]}, asked)
    # Someone the user didn't type (from a mail the agent read) still asks, and so does a message they didn't ask to send.
    assert not asked_in_chat("send_message", {"to": ["friend@example.com", "other@evil.test"]}, asked)
    assert not asked_in_chat("send_message", {"to": ["friend@example.com"]}, "who is friend@example.com?")
    assert not asked_in_chat("delete_message", {"to": ["friend@example.com"]}, asked)
    assert not asked_in_chat("send_message", {"to": []}, asked)


@needs_db
async def test_the_app_edits_and_deletes_a_meeting_trigger(factory):
    from app.automations.routes import AutomationPatch, patch_automation

    user_id = await _user(factory)
    made = await _make(factory, user_id, trigger_type="meeting")
    aid = made["automation"]["id"]
    async with factory() as db:
        user = await db.get(User, user_id)
        out = await patch_automation(aid, AutomationPatch(action="Send me a short pre-read", trigger="30", requires_approval=False), user, db)
    assert out["action"] == "Send me a short pre-read" and out["trigger"] == "30" and out["requires_approval"] is False
    async with factory() as db:
        user = await db.get(User, user_id)
        out = await patch_automation(aid, AutomationPatch(enabled=False), user, db)
    assert out["enabled"] is False


@needs_db
async def test_a_routine_can_be_renamed_and_rewritten(factory):
    from app.tasks.service import create_from_prompt, edit_task

    user_id = await _user(factory)
    async with factory() as db:
        user = await db.get(User, user_id)
        task = await create_from_prompt(db, user, "every day at 9am brief me on my inbox")
        out = await edit_task(db, user, __import__("uuid").UUID(task["id"]), title="Morning brief", instruction="Brief me on unread mail only")
    assert out["title"] == "Morning brief" and out["instruction"] == "Brief me on unread mail only"
    assert out["schedule_label"] == task["schedule_label"]  # when it runs stays
