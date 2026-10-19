from app.agent.confirm import read_confirm, sign_confirm


def test_confirm_roundtrip():
    token = sign_confirm("tool", {"slug": "gmail", "tool": "send_message", "args": {"to": ["a@b.com"]}})
    data = read_confirm(token)
    assert data["kind"] == "tool"
    assert data["payload"]["slug"] == "gmail"
    assert read_confirm("nope") is None
