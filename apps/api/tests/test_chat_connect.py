"""Connect a remote MCP server from chat (app/connectors/chat_connect.py)."""

import json
import uuid
from types import SimpleNamespace

import pytest

from app.agent import deep
from app.agent.confirm import read_confirm
from app.connectors import chat_connect as cc
from app.connectors.urlsafety import UnsafeUrl

URL = "https://mcp.example.org/mcp"


class Store:
    """In-memory stand-in for the connector rows and tool index."""

    def __init__(self):
        self.rows: list[SimpleNamespace] = []
        self.indexed: dict[str, list] = {}
        self.synced: list = []

    def install(self, monkeypatch, *, tools=None, error=None, meta=None, reg=None, resync_error=None):
        calls = {"probe": 0, "register": 0}

        async def validate(url):
            if "internal" in url:
                raise UnsafeUrl("URL must resolve to a public address")
            return url

        async def probe(url, creds):
            calls["probe"] += 1
            if error is not None:
                raise error
            return tools or []

        async def find(db, user_id, url):
            return next((r for r in self.rows if cc._key(r.server_url) == cc._key(url)), None)

        async def save(db, user, url, name, existing, auth_type, creds):
            row = existing
            if row is None:
                label = name or url.split("/")[2].replace(".", "-")
                row = SimpleNamespace(id=uuid.uuid4(), name=label, alias=None, slug=label, server_url=url,
                                      server_key=f"mcp:{url}", health="pending", credentials_enc=None)  # fmt: skip
                self.rows.append(row)
            row.auth_type, row.creds, row.health = auth_type, creds, "pending"
            return row

        async def index(db, row, listed):
            self.indexed[row.server_key] = listed
            row.health = "ok"

        async def count(db, row):
            listed = self.indexed.get(row.server_key)
            return None if listed is None else len(listed)

        async def discover(url):
            return meta or {}

        async def register(endpoint):
            calls["register"] += 1
            return reg or {}

        async def resync(db, row):
            self.synced.append(row.id)
            if resync_error is not None:
                raise resync_error
            row.health = "ok"

        monkeypatch.setattr(cc, "validate_public_url", validate)
        monkeypatch.setattr(cc, "probe_tools", probe)
        monkeypatch.setattr(cc, "find_existing", find)
        monkeypatch.setattr(cc, "save_row", save)
        monkeypatch.setattr(cc, "index_tools", index)
        monkeypatch.setattr(cc, "tool_count", count)
        monkeypatch.setattr(cc, "discover_auth", discover)
        monkeypatch.setattr(cc, "register_client", register)
        monkeypatch.setattr(cc, "resync", resync)
        return calls


def _ctx(query=f"connect to {URL}", **kw):
    base = dict(
        db=object(), user=SimpleNamespace(id=uuid.uuid4()), agent=None, query=query, confirmations=[], pending=[],
        results=[], connectors={}, injections=[], auto_confirm=False, job=None, used_tools=[],
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _tools(*names):
    return [SimpleNamespace(name=n) for n in names]


def test_named_in_message_matches_spellings_not_neighbours():
    assert cc.named_in_message(URL, f"please connect to {URL}")
    assert cc.named_in_message(URL, "connect mcp.example.org/mcp/ for me")
    assert cc.named_in_message(URL, f"add <{URL}>.")
    assert not cc.named_in_message(URL, "connect to https://mcp.example.org/mcp2")
    assert not cc.named_in_message(URL, "connect my MCP server")


def test_needs_auth_reads_status_inside_exception_groups():
    resp = SimpleNamespace(status_code=401)
    err = type("HTTPStatusError", (Exception,), {})("Client error")
    err.response = resp
    assert cc.needs_auth(ExceptionGroup("tg", [err]))
    assert cc.needs_auth(RuntimeError("403 Forbidden"))
    assert not cc.needs_auth(ConnectionError("name or service not known"))
    assert not cc.needs_auth(None)


async def test_url_in_users_message_connects_without_a_card(monkeypatch):
    store = Store()
    store.install(monkeypatch, tools=_tools("search_notes", "add_note", "list_cards"))
    ctx = _ctx()
    out = await cc.connect_mcp_server(ctx, URL)
    assert out["status"] == "connected"
    assert out["tool_count"] == 3 and out["connector"] == "mcp-example-org"
    assert "3 tools" in out["message"]
    assert ctx.pending == []
    assert store.rows[0].auth_type == "none"
    assert ctx.connectors[out["slug"]] is store.rows[0]


async def test_url_without_scheme_is_https(monkeypatch):
    Store().install(monkeypatch, tools=_tools("a"))
    out = await cc.connect_mcp_server(_ctx(query="connect mcp.example.org/mcp"), "mcp.example.org/mcp")
    assert out["status"] == "connected"


async def test_url_the_user_did_not_type_needs_approval(monkeypatch):
    store = Store()
    calls = store.install(monkeypatch, tools=_tools("a"))
    ctx = _ctx(query="connect the server from that doc")
    out = await cc.connect_mcp_server(ctx, URL)
    assert out["status"] == "awaiting_user_confirmation"
    assert calls["probe"] == 0 and store.rows == []
    payload = read_confirm(ctx.pending[0]["token"])["payload"]
    assert payload == {"slug": cc.CONNECTORS_SLUG, "tool": cc.TOOL, "args": {"url": URL, "name": ""}}

    done = await cc.run_confirmed(_ctx(query="yes"), payload["tool"], payload["args"])
    assert done["status"] == "connected" and done["tool_count"] == 1


async def test_approved_card_in_this_turn_is_not_asked_again(monkeypatch):
    Store().install(monkeypatch, tools=_tools("a", "b"))
    args = {"url": URL, "name": ""}
    ctx = _ctx(query="go ahead", confirmations=[{"slug": cc.CONNECTORS_SLUG, "tool": cc.TOOL, "args": args}])
    out = await cc.connect_mcp_server(ctx, URL)
    assert out["status"] == "connected" and ctx.pending == []


async def test_injected_content_forces_a_card(monkeypatch):
    Store().install(monkeypatch, tools=_tools("a"))
    ctx = _ctx(injections=[{"source": "web_read"}])
    out = await cc.connect_mcp_server(ctx, URL)
    assert out["status"] == "awaiting_user_confirmation"


async def test_unattended_runs_never_add_a_connector(monkeypatch):
    store = Store()
    store.install(monkeypatch, tools=_tools("a"))
    out = await cc.connect_mcp_server(_ctx(auto_confirm=True), URL)
    assert "approval" in out["error"] and store.rows == []


async def test_unsafe_url_is_refused(monkeypatch):
    store = Store()
    store.install(monkeypatch, tools=_tools("a"))
    out = await cc.connect_mcp_server(_ctx(query="connect https://internal.example/mcp"), "https://internal.example/mcp")
    assert "can't be used" in out["error"] and store.rows == []


async def test_oauth_server_gets_a_sign_in_card(monkeypatch):
    store = Store()
    meta = {"authorization_endpoint": "https://auth.example.org/authorize", "token_endpoint": "https://auth.example.org/token",
            "registration_endpoint": "https://auth.example.org/register"}  # fmt: skip
    resp = SimpleNamespace(status_code=401)
    err = Exception("unauthorized")
    err.response = resp
    store.install(monkeypatch, error=err, meta=meta, reg={"client_id": "cid", "client_secret": None})
    ctx = _ctx()
    out = await cc.connect_mcp_server(ctx, URL)
    assert out["status"] == "needs_sign_in"
    row = store.rows[0]
    assert row.auth_type == "oauth" and row.creds["pending"] and row.creds["client_id"] == "cid"
    cards = deep._connect_cards(SimpleNamespace(results=ctx.results, agent=None))
    assert len(cards) == 1
    assert cards[0]["redirect_url"] == f"/api/connectors/{row.id}/oauth/start"


async def test_key_server_gets_a_key_card_and_no_secret_in_chat(monkeypatch):
    store = Store()
    store.install(monkeypatch, error=RuntimeError("HTTP 401 Unauthorized: missing API key"))
    ctx = _ctx()
    out = await cc.connect_mcp_server(ctx, URL)
    assert out["status"] == "needs_api_key"
    assert "never into the chat" in out["message"]
    cards = deep._connect_cards(SimpleNamespace(results=ctx.results, agent=None))
    assert cards[0]["needs_credentials"] is True
    assert ctx.results[0]["data"]["connector_id"] == str(store.rows[0].id)
    assert store.rows[0].auth_type == "bearer" and store.rows[0].creds is None


async def test_unreachable_server_saves_nothing(monkeypatch):
    store = Store()
    store.install(monkeypatch, error=ConnectionError("name or service not known"))
    out = await cc.connect_mcp_server(_ctx(), URL)
    assert "Could not connect" in out["error"] and store.rows == []


async def test_already_connected_reports_tool_count(monkeypatch):
    store = Store()
    calls = store.install(monkeypatch, tools=_tools("a", "b"))
    await cc.connect_mcp_server(_ctx(), URL)
    out = await cc.connect_mcp_server(_ctx(), URL + "/")
    assert out["status"] == "already_connected" and out["tool_count"] == 2
    assert calls["probe"] == 1 and len(store.rows) == 1


async def test_existing_keyed_connector_resyncs_with_its_key(monkeypatch):
    store = Store()
    store.install(monkeypatch, tools=_tools("a"))
    row = SimpleNamespace(id=uuid.uuid4(), name="srv", alias=None, slug="srv", server_url=URL, server_key=f"mcp:{URL}",
                          health="down", credentials_enc=b"x", auth_type="bearer")  # fmt: skip
    store.rows.append(row)
    monkeypatch.setattr(cc, "has_credentials", lambda r: True)
    out = await cc.connect_mcp_server(_ctx(), URL)
    assert out["status"] == "connected" and store.synced == [row.id]
    assert row.auth_type == "bearer"  # the stored key was kept


def test_tool_is_registered_and_described():
    ctx = deep.RunContext(db=None, user=SimpleNamespace(id=uuid.uuid4()), connectors={})
    tools = {t.name: t for t in deep.harness_tools(ctx)}
    assert "connect_mcp_server" in tools
    assert "MCP" in tools["connect_mcp_server"].description


def test_a_message_with_a_server_url_is_not_a_marketplace_connect():
    assert deep._is_connect_query("connect gmail")
    assert not deep._is_connect_query(f"connect to {URL}")


async def test_harness_tool_returns_json(monkeypatch):
    Store().install(monkeypatch, tools=_tools("a"))
    ctx = deep.RunContext(db=object(), user=SimpleNamespace(id=uuid.uuid4()), connectors={})
    ctx.query = f"connect {URL}"
    tool = next(t for t in deep.harness_tools(ctx) if t.name == "connect_mcp_server")
    out = json.loads(await tool.ainvoke({"url": URL}))
    assert out["status"] == "connected" and out["tool_count"] == 1


@pytest.mark.parametrize("bad", ["", "ftp://x.org/mcp"])
async def test_normalize_keeps_bad_urls_bad(monkeypatch, bad):
    out = await cc.connect_mcp_server(_ctx(query=f"connect {bad}"), bad)
    assert "error" in out


class FakeDb:
    def __init__(self, row):
        self.row, self.commits = row, 0

    async def get(self, model, key):
        return self.row if getattr(model, "__name__", "") == "Connector" else None

    async def commit(self):
        self.commits += 1


def _mcp_row(user_id):
    return SimpleNamespace(id=uuid.uuid4(), user_id=user_id, kind="mcp", name="srv", alias=None, description="", slug="srv",
                           provider="mcp", enabled=True, health="pending", health_detail="", tags=[], default_for=[],
                           server_url=URL, auth_type="bearer", server_key=None, credentials_enc=None)  # fmt: skip


async def test_credentials_route_stores_key_and_syncs(monkeypatch):
    from fastapi import HTTPException

    from app.connectors import routes
    from app.security import decrypt_json

    user = SimpleNamespace(id=uuid.uuid4())
    row = _mcp_row(user.id)
    synced = []

    async def sync(db, r):
        synced.append(r.id)
        r.health = "ok"

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(routes, "sync_connector", sync)
    monkeypatch.setattr(routes, "invalidate", noop)
    out = await routes.save_credentials(row.id, routes.CredentialsBody(token=" sk-123 "), user, FakeDb(row))
    assert out["connected"] and synced == [row.id]
    creds = decrypt_json(row.credentials_enc)
    assert creds["access_token"] == "sk-123" and creds.get("prefix") == "Bearer "

    out = await routes.save_credentials(row.id, routes.CredentialsBody(token="k", header="X-API-Key"), user, FakeDb(row))
    assert row.auth_type == "header" and decrypt_json(row.credentials_enc)["header"] == "X-API-Key"

    with pytest.raises(HTTPException) as other:
        await routes.save_credentials(row.id, routes.CredentialsBody(token="k"), SimpleNamespace(id=uuid.uuid4()), FakeDb(row))
    assert other.value.status_code == 404

    async def refused(db, r):
        raise RuntimeError("401 Unauthorized")

    monkeypatch.setattr(routes, "sync_connector", refused)
    with pytest.raises(HTTPException) as bad:
        await routes.save_credentials(row.id, routes.CredentialsBody(token="wrong"), user, FakeDb(row))
    assert bad.value.status_code == 400 and "did not accept" in bad.value.detail
