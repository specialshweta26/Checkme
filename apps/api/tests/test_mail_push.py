"""New Outlook mail wakes the inbox check, and only real notifications count."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.automations.routes import webhook_router
from app.db import get_db
from app.inbox import push, worker

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")
NOW = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)
HOOK = "https://app.test/api/webhooks/microsoft-mail"


@pytest.fixture
def client(monkeypatch):
    woken = []
    monkeypatch.setattr(worker, "wake_inbox", lambda: woken.append(1))
    valid = {"count": 0}

    async def accept(db, payload):
        return valid["count"]

    monkeypatch.setattr(push, "accept_notifications", accept)
    app = FastAPI()
    app.include_router(webhook_router, prefix="/api")

    async def no_db():
        yield None

    app.dependency_overrides[get_db] = no_db
    return SimpleNamespace(http=TestClient(app), woken=woken, valid=valid)


def test_graph_validation_echoes_the_token_as_plain_text(client):
    resp = client.http.post("/api/webhooks/microsoft-mail?validationToken=abc%20123")
    assert resp.status_code == 200 and resp.text == "abc 123"
    assert resp.headers["content-type"].startswith("text/plain")
    assert client.woken == []


def test_a_valid_notification_wakes_the_inbox(client):
    client.valid["count"] = 1
    assert client.http.post("/api/webhooks/microsoft-mail", json={"value": []}).status_code == 202
    assert client.woken == [1]


def test_a_bad_notification_gets_the_same_answer_and_no_wake(client):
    assert client.http.post("/api/webhooks/microsoft-mail", content=b"not json").status_code == 202
    assert client.http.post("/api/webhooks/microsoft-mail", json={"value": []}).status_code == 202
    assert client.woken == []


def test_a_huge_notification_is_refused(client):
    assert client.http.post("/api/webhooks/microsoft-mail", content=b"x" * (push.MAX_BODY_BYTES + 1)).status_code == 413


def test_ordinary_webhook_paths_still_route_to_automations(client):
    assert client.http.post("/api/webhooks/not-a-number", json={}).status_code == 422


async def test_a_wake_runs_the_check_early_and_the_timer_still_runs(monkeypatch):
    monkeypatch.setattr(worker, "WAKE_GAP_SEC", 0)
    monkeypatch.setattr(worker, "_wake", asyncio.Event())
    monkeypatch.setattr(worker, "INTERVAL_SEC", 60)
    waiting = asyncio.create_task(worker._wait_for_next_check())
    await asyncio.sleep(0)
    worker.wake_inbox()
    await asyncio.wait_for(waiting, 1)
    assert not worker._wake.is_set()

    monkeypatch.setattr(worker, "INTERVAL_SEC", 0.01)
    await asyncio.wait_for(worker._wait_for_next_check(), 1)


@pytest.mark.parametrize("payload", [None, [], "x", {"value": "x"}, {"value": ["x"]}])
async def test_malformed_payloads_count_for_nothing(payload):
    class Db:
        async def execute(self, q):
            raise AssertionError("must not query")

    assert await push.accept_notifications(Db(), payload) == 0


async def test_sync_does_nothing_without_https(monkeypatch):
    monkeypatch.setattr(push, "get_settings", lambda: SimpleNamespace(microsoft_mail_push=True, public_web_url="http://localhost:3000"))
    assert await push.sync_all(None) == 0


async def test_sync_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(push, "get_settings", lambda: SimpleNamespace(microsoft_mail_push=False, public_web_url="https://app.test"))
    assert await push.sync_all(None) == 0


@pytest.fixture
async def factory():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Base

    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _connector(factory):
    from app.models import Account, Connector, User

    async with factory() as db:
        user = User(email=f"{uuid.uuid4().hex[:8]}@x.test", primary_provider="microsoft")
        db.add(user)
        await db.flush()
        account = Account(user_id=user.id, provider="microsoft", subject=uuid.uuid4().hex, email=user.email)
        db.add(account)
        await db.flush()
        row = Connector(user_id=user.id, kind="builtin", provider="outlook", account_id=account.id, name="Outlook", slug=f"o{uuid.uuid4().hex[:8]}")
        db.add(row)
        await db.commit()
        return row


class Graph:
    """Stands in for Microsoft Graph."""

    def __init__(self, patch_status=200):
        self.calls = []
        self.patch_status = patch_status
        self.prefix = uuid.uuid4().hex[:8]  # subscription ids are unique across test runs
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        body = json.loads(request.content or b"{}")
        self.calls.append((request.method, str(request.url), body))
        if request.method == "POST":
            return httpx.Response(201, json={"id": f"{self.prefix}-{len(self.calls)}"})
        return httpx.Response(self.patch_status, json={})


@pytest.fixture
def no_tokens(monkeypatch):
    async def token(account_id):
        return "token"

    monkeypatch.setattr("app.auth.tokens.account_access_token", token)


@needs_db
async def test_a_mailbox_is_subscribed_then_left_alone_then_renewed(factory, no_tokens):
    from app.models import MailSubscription

    conn, graph = await _connector(factory), Graph()
    async with factory() as db:
        assert await push.sync_connector(db, conn, HOOK, NOW, graph.client) == "created"
    method, url, body = graph.calls[0]
    assert method == "POST" and url.endswith("/subscriptions")
    assert body["notificationUrl"] == HOOK and body["changeType"] == "created" and "Inbox" in body["resource"]
    secret = body["clientState"]

    async with factory() as db:
        row = (await db.execute(select(MailSubscription).where(MailSubscription.connector_id == conn.id))).scalar_one()
        assert secret not in (row.client_state_hash, row.subscription_id)  # only its hash is stored
        assert await push.sync_connector(db, conn, HOOK, NOW + timedelta(hours=1), graph.client) == "ok"
    assert len(graph.calls) == 1

    later = NOW + push.LIFETIME - timedelta(hours=1)
    async with factory() as db:
        assert await push.sync_connector(db, conn, HOOK, later, graph.client) == "renewed"
    assert graph.calls[1][0] == "PATCH"
    async with factory() as db:
        row = (await db.execute(select(MailSubscription).where(MailSubscription.connector_id == conn.id))).scalar_one()
        assert row.expires_at == later + push.LIFETIME


@needs_db
async def test_a_lapsed_subscription_is_replaced(factory, no_tokens):
    from app.models import MailSubscription

    conn, graph = await _connector(factory), Graph(patch_status=404)
    async with factory() as db:
        await push.sync_connector(db, conn, HOOK, NOW, graph.client)
    async with factory() as db:
        assert await push.sync_connector(db, conn, HOOK, NOW + push.LIFETIME, graph.client) == "recreated"
    async with factory() as db:
        rows = (await db.execute(select(MailSubscription).where(MailSubscription.connector_id == conn.id))).scalars().all()
    assert len(rows) == 1 and rows[0].subscription_id == f"{graph.prefix}-3"


@needs_db
async def test_only_the_right_secret_counts(factory, no_tokens):
    conn, graph = await _connector(factory), Graph()
    async with factory() as db:
        await push.sync_connector(db, conn, HOOK, NOW, graph.client)
    secret = graph.calls[0][2]["clientState"]
    sub_id = f"{graph.prefix}-1"
    good = {"subscriptionId": sub_id, "clientState": secret}
    async with factory() as db:
        assert await push.accept_notifications(db, {"value": [good]}) == 1
        assert await push.accept_notifications(db, {"value": [{**good, "clientState": "wrong"}]}) == 0
        assert await push.accept_notifications(db, {"value": [{"subscriptionId": sub_id}]}) == 0
        assert await push.accept_notifications(db, {"value": [{**good, "subscriptionId": "unknown"}]}) == 0


@needs_db
async def test_one_failing_mailbox_does_not_stop_the_others(factory, monkeypatch):
    bad, good = await _connector(factory), await _connector(factory)

    async def token(account_id):
        if account_id == bad.account_id:
            raise RuntimeError("reconnect needed")
        return "token"

    monkeypatch.setattr("app.auth.tokens.account_access_token", token)
    monkeypatch.setattr(push, "get_settings", lambda: SimpleNamespace(microsoft_mail_push=True, public_web_url="https://app.test"))
    async with factory() as db:
        await db.execute(text("UPDATE connectors SET enabled = false WHERE id NOT IN (:a, :b)"), {"a": bad.id, "b": good.id})
        await db.commit()
    assert await push.sync_all(factory, NOW, Graph().client) == 1
