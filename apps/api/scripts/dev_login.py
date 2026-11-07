"""Local test sign-in without Google or Microsoft: creates (or reuses) a test user and prints a session cookie.

Only for a local stack: it refuses to run unless PUBLIC_WEB_URL and DATABASE_URL point at this machine (or the
compose network). The user has no mailbox attached, so mail and calendar tools are not available; chat, agents and
memory work.

    docker compose exec api python scripts/dev_login.py            # prints pa_session=<token>
    docker compose exec api python scripts/dev_login.py --email other@checkme.test

Set the printed cookie on http://localhost:3000 (browser devtools, or curl -b) to be signed in as that user.
"""

import argparse
import asyncio
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.agents.service import ensure_default_agent  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import AuthSession, User  # noqa: E402
from app.security import SESSION_COOKIE, issue_session_jwt, session_expiry  # noqa: E402

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres", "host.docker.internal"}


def _local(url: str) -> bool:
    host = (urlparse(url.replace("+asyncpg", "")).hostname or "").lower()
    return host in LOCAL_HOSTS or host.endswith(".localhost") or host.endswith(".test")


async def main(email: str, name: str) -> None:
    s = get_settings()
    if not (_local(s.public_web_url) and _local(s.database_url)):
        raise SystemExit("dev_login only runs against a local stack (PUBLIC_WEB_URL and DATABASE_URL must be local).")
    if not email.endswith(".test"):
        raise SystemExit("Use an address under a .test domain, e.g. test@checkme.test.")
    await init_db()
    async with SessionLocal() as db:
        user = (await db.execute(select(User).where(User.email == email))).scalars().first()
        if user is None:
            user = User(email=email, display_name=name, primary_provider="google", timezone="Asia/Kolkata")
            db.add(user)
            await db.flush()
        await ensure_default_agent(db, user)
        sess = AuthSession(user_id=user.id, expires_at=session_expiry(), user_agent="dev_login")
        db.add(sess)
        await db.commit()
        print(f"user_id={user.id}")
        print(f"{SESSION_COOKIE}={issue_session_jwt(user.id, sess.id, sess.expires_at)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", default="test@checkme.test")
    parser.add_argument("--name", default="Test User")
    args = parser.parse_args()
    asyncio.run(main(args.email, args.name))
