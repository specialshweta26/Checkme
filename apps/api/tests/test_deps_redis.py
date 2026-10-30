import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.deps import optional_user
from app.security import SESSION_COOKIE, issue_session_jwt, session_expiry


class BoomRedis:
    async def get(self, _k):
        raise ConnectionError("Error 111 connecting to redis. Connection refused.")

    async def set(self, *_a, **_k):
        raise ConnectionError("refused")


class FakeDB:
    def __init__(self, objects):
        self.objects = objects

    async def get(self, _model, key):
        return self.objects.get(key)


async def test_optional_user_uses_postgres_when_redis_is_down(monkeypatch):
    from app import deps

    monkeypatch.setattr(deps, "get_redis", lambda: BoomRedis())
    uid, sid = uuid.uuid4(), uuid.uuid4()
    user = SimpleNamespace(id=uid)
    sess = SimpleNamespace(id=sid, revoked=False, expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    token = issue_session_jwt(uid, sid, session_expiry())
    request = SimpleNamespace(cookies={SESSION_COOKIE: token}, state=SimpleNamespace())
    got = await optional_user(request, FakeDB({sid: sess, uid: user}))
    assert got is user
    assert request.state.auth_session_id == sid


async def test_optional_user_without_cookie_skips_redis():
    request = SimpleNamespace(cookies={}, state=SimpleNamespace())
    assert await optional_user(request, FakeDB({})) is None
