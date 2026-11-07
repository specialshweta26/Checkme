"""Export and delete your account, against a real Postgres: the export has your data and none of anyone else's and no
credentials; deleting needs your email typed, removes every row with your user_id, your chat checkpoints, files and
computers, and leaves other users untouched.

Skipped unless CHECKME_TEST_DATABASE_URL is set (see tests/test_user_memory_db.py).
"""

from __future__ import annotations

import io
import json
import os
import zipfile

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text

from tests.test_user_memory_db import _user_with_agents, factory  # noqa: F401 - the fixture is used by name

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")


def _client(factory, user):
    from app.account.routes import router
    from app.db import get_db
    from app.deps import current_user

    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def db_dep():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db] = db_dep
    app.dependency_overrides[current_user] = lambda: user
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app.test")


@pytest.fixture
def outside(monkeypatch):
    """Files and computers: recorded, so the test can see what deletion asked for."""
    from app import storage
    from app.connectors import e2b_box

    files = {}
    deleted, killed = [], []
    monkeypatch.setattr(storage, "list_keys", lambda prefix: [k for k in files if k.startswith(prefix)])
    monkeypatch.setattr(storage, "get_bytes", lambda key: (files[key], "text/plain"))
    monkeypatch.setattr(storage, "delete_keys", lambda keys: deleted.extend(keys))
    monkeypatch.setattr(e2b_box, "kill_user_sandboxes", lambda uid: killed.append(uid) or 1)

    async def no_letta(agent, user=None):
        return None

    monkeypatch.setattr("app.agent.memory.forget_letta_agent", no_letta)
    return type("O", (), {"files": files, "deleted": deleted, "killed": killed})


async def _seed(factory, user, chief):
    from app.agent import memory
    from app.models import Account, ChatSession

    async with factory() as db:
        db.add(Account(user_id=user.id, provider="google", subject=f"g-{user.id.hex[:8]}", email=user.email,
                       refresh_token_enc="SECRET-REFRESH"))
        sess = ChatSession(user_id=user.id, title="Hello")
        db.add(sess)
        await db.commit()
        await memory.remember(db, user.id, "archival", f"Fact for {user.email}", chief.id)
        await db.execute(text("INSERT INTO run_checkpoints (thread_id, ns, checkpoint_id, checkpoint_type, checkpoint, meta, updated_at) "
                              "VALUES (:t, '', 'c1', 'json', '\\x00'::bytea, '\\x00'::bytea, now()) ON CONFLICT DO NOTHING"), {"t": str(sess.id)})
        await db.commit()
        return sess.id


async def _rows_for(factory, user_id) -> dict:
    from app.account.routes import _user_tables

    out = {}
    async with factory() as db:
        for table in _user_tables():
            n = (await db.execute(select(table.c.user_id).where(table.c.user_id == user_id))).all()
            if n:
                out[table.name] = len(n)
    return out


async def test_export_has_my_data_only_and_no_credentials(factory, outside):
    a, (chief_a,) = await _user_with_agents(factory)
    b, (chief_b,) = await _user_with_agents(factory)
    await _seed(factory, a, chief_a)
    await _seed(factory, b, chief_b)
    outside.files[f"{a.id}/x1/notes.txt"] = b"my notes"
    outside.files[f"{b.id}/x2/theirs.txt"] = b"not mine"
    async with _client(factory, a) as http:
        resp = await http.get("/api/me/export")
    assert resp.status_code == 200 and resp.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    names = set(zf.namelist())
    assert "data/memory_passages.json" in names and "files/x1/notes.txt" in names and "files/x2/theirs.txt" not in names
    blob = b"".join(zf.read(n) for n in names)
    assert f"Fact for {a.email}".encode() in blob and f"Fact for {b.email}".encode() not in blob
    assert b"SECRET-REFRESH" not in blob and b"refresh_token_enc" not in blob  # no credentials
    assert json.loads(zf.read("data/user.json"))[0]["email"] == a.email
    fact = json.loads(zf.read("data/memory_passages.json"))[0]
    assert "embedding" not in fact and "search_tsv" not in fact  # internal, large columns are left out


async def test_delete_needs_my_email_and_removes_everything_of_mine_only(factory, outside):
    a, (chief_a,) = await _user_with_agents(factory)
    b, (chief_b,) = await _user_with_agents(factory)
    sess_a = await _seed(factory, a, chief_a)
    sess_b = await _seed(factory, b, chief_b)
    outside.files[f"{a.id}/x1/notes.txt"] = b"my notes"
    b_before = await _rows_for(factory, b.id)
    assert await _rows_for(factory, a.id)
    async with _client(factory, a) as http:
        assert (await http.post("/api/me/delete", json={"confirm": "someone@else.test"})).status_code == 400
        assert await _rows_for(factory, a.id)  # nothing happened
        resp = await http.post("/api/me/delete", json={"confirm": a.email.upper()})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert "pa_session=" in resp.headers.get("set-cookie", "")  # signed out
    assert await _rows_for(factory, a.id) == {}
    assert await _rows_for(factory, b.id) == b_before
    from app.models import User

    async with factory() as db:
        assert await db.get(User, a.id) is None and await db.get(User, b.id) is not None
        cps = {r[0] for r in (await db.execute(text("SELECT thread_id FROM run_checkpoints WHERE thread_id IN (:a, :b)"),
                                               {"a": str(sess_a), "b": str(sess_b)})).all()}
    assert cps == {str(sess_b)}
    assert outside.deleted == [f"{a.id}/x1/notes.txt"] and outside.killed == [a.id]
