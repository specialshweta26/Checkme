"""Pop-ups a page shows reach the agent, however long the task."""

from types import SimpleNamespace

from app.agent import computer_harness as ch, fast_browser as fb


def test_each_popup_of_the_turn_is_kept():
    ctx = SimpleNamespace()
    ch.note_popups(ctx, {"dialogs": [{"type": "alert", "message": "Product added", "answer": "declined"}]})
    ch.note_popups(ctx, {"ok": True})
    ch.note_popups(ctx, {"dialogs": [{"type": "alert", "message": "Product added", "answer": "declined"}]})
    assert [p["message"] for p in ctx._popups] == ["Product added", "Product added"]


def test_a_task_reports_only_its_own_popups():
    ctx = SimpleNamespace(_popups=[{"type": "alert", "message": "earlier", "answer": "declined"}])
    ch.note_popups(ctx, {"dialogs": [{"type": "alert", "message": "Product added", "answer": "declined"}]})
    out = fb._with_popups(ctx, {"ok": True}, since=1)
    assert out["popups"] == [{"type": "alert", "message": "Product added", "answer": "declined"}]
    assert "popups" not in fb._with_popups(ctx, {"ok": True}, since=2)


def test_the_step_summary_shows_popups():
    brief = fb.page_brief({"url": "u", "dialogs": [{"type": "alert", "message": "Product added"}]})
    assert "Pop-ups after the last step: alert: Product added" in brief


async def test_a_replayed_run_keeps_its_popups():
    seen = {"dialogs": [{"type": "alert", "message": "Product added", "answer": "closed"}], "ok": True}

    async def impl(slug, name, args):
        return seen

    ctx = SimpleNamespace(call_impl=impl, connectors={"computer": object()})
    import app.agent.computer_harness as harness

    original = harness.computer_slug
    harness.computer_slug = lambda c: "computer"
    try:
        await fb._internal(ctx, "browser_replay", {"steps": []})
    finally:
        harness.computer_slug = original
    assert ctx._popups[0]["message"] == "Product added"

