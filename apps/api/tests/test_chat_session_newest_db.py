"""A long chat shows its newest messages (they vanished once a chat passed 200 messages)."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text

URL = os.environ.get("CHECKME_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="CHECKME_TEST_DATABASE_URL not set")


async def test_a_chat_over_200_messages_returns_its_newest_in_order():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.chat import routes
    from app.models import Base, ChatMessage, ChatSession, User

    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    uid = uuid.uuid4()
    try:
        async with factory() as db:
            user = User(id=uid, email=f"{uid.hex[:8]}@x.test", primary_provider="google")
            db.add(user)
            await db.flush()
            sess = ChatSession(user_id=uid, title="long chat")
            db.add(sess)
            await db.flush()
            for i in range(260):
                db.add(ChatMessage(session_id=sess.id, user_id=uid, role="user" if i % 2 == 0 else "assistant", content=f"m{i}", meta={}))
            await db.commit()
            out = await routes.session_messages(sess.id, user=user, db=db)
        contents = [m["content"] for m in out["messages"]]
        assert len(contents) == routes.SESSION_MESSAGES == 200
        assert contents[-1] == "m259" and contents[0] == "m60"  # the newest 200, oldest first
        assert contents == [f"m{i}" for i in range(60, 260)]
    finally:
        async with factory() as db:
            await db.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
            await db.commit()
        await engine.dispose()
