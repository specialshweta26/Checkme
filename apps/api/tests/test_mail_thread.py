from outlook_mcp.connector import OutlookConnector

CONV = "AAQkConvAdidas"
LATEST = "AAMkMsgLatest"
FIRST = "AAMkMsgFirst"


def _msg(mid: str, body: str, when: str, subject: str = "Re: Adidas Data Engineer Profiles <> Scrabble."):
    return {
        "id": mid,
        "conversationId": CONV,
        "subject": subject,
        "from": {"emailAddress": {"name": "Gourishanker Jha", "address": "gourishanker.jha@agilitas.com"}},
        "receivedDateTime": when,
        "body": {"contentType": "text", "content": body},
        "toRecipients": [{"emailAddress": {"name": "Gaurav", "address": "gaurav.ranjan@agilitas.com"}}],
        "ccRecipients": [],
        "hasAttachments": False,
        "bodyPreview": body[:80],
        "isRead": True,
    }


class FakeApi:
    def __init__(self, by_id: dict, conversation: list[dict], folders: dict | None = None):
        self.by_id = by_id
        self.conversation = conversation
        self.folders = folders or {}
        self.calls = []

    async def json(self, method, url, params=None, headers=None, json=None):
        self.calls.append((method, url, params or {}))
        if "/messages/" in url and not url.rstrip("/").endswith("/messages"):
            for mid, row in self.by_id.items():
                if mid in url:
                    return row
            raise RuntimeError(f"unknown message {url}")
        if "/mailFolders/" in url:
            fid = url.split("/mailFolders/")[1].split("/")[0]
            return {"value": list(self.folders.get(fid, []))}
        return {"value": list(self.conversation)}


def _connector(api: FakeApi) -> OutlookConnector:
    async def token():
        return "tok"

    conn = OutlookConnector(token)
    conn.api = api
    return conn


async def test_outlook_get_thread_resolves_message_id_and_lists_conversation():
    first = _msg(FIRST, "Please find the Adidas profiles.", "2026-10-19T10:00:00Z", "Adidas Data Engineer Profiles <> Scrabble.")
    latest = _msg(LATEST, "Please take this forward.", "2026-10-20T13:46:00Z")
    api = FakeApi({FIRST: first, LATEST: latest}, [first, latest])
    out = await _connector(api).tool_get_thread(thread_id="", message_id=LATEST)
    assert out["thread_id"] == CONV
    assert out["message_count"] == 2
    assert [m["message_id"] for m in out["messages"]] == [FIRST, LATEST]
    assert "Please find the Adidas profiles." in out["messages"][0]["body"]


async def test_outlook_get_thread_treats_graph_message_id_as_seed():
    first = _msg(FIRST, "Original request", "2026-10-19T10:00:00Z", "Adidas Data Engineer Profiles <> Scrabble.")
    latest = _msg(LATEST, "Please take this forward.", "2026-10-20T13:46:00Z")
    api = FakeApi({FIRST: first, LATEST: latest}, [first, latest])
    out = await _connector(api).tool_get_thread(thread_id=LATEST)
    assert out["thread_id"] == CONV
    assert out["message_count"] == 2


async def test_outlook_get_thread_keeps_quoted_history_when_only_one_message():
    body = "Please take this forward.\n\nOn Mon, Regina wrote:\n> Please find the Adidas profiles."
    latest = _msg(LATEST, body, "2026-10-20T13:46:00Z")
    api = FakeApi({LATEST: latest}, [latest])
    out = await _connector(api).tool_get_thread(thread_id=CONV, message_id=LATEST)
    assert out["message_count"] == 1
    assert "Adidas profiles" in out["messages"][0]["body"]


async def test_outlook_search_fills_missing_conversation_id():
    latest = _msg(LATEST, "Please take this forward.", "2026-10-20T13:46:00Z")
    listed = {**latest, "conversationId": ""}
    api = FakeApi({LATEST: latest}, [listed])
    out = await _connector(api).tool_search_messages("Adidas Data Engineer")
    assert out["messages"][0]["thread_id"] == CONV
    assert out["messages"][0]["message_id"] == LATEST
