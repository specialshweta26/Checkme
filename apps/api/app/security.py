from __future__ import annotations

import base64
import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadData, URLSafeTimedSerializer

from app.config import get_settings

SESSION_COOKIE = "pa_session"
OAUTH_COOKIE = "pa_oauth"


def _fernet() -> Fernet:
    key = get_settings().token_encryption_key
    if not key:
        key = base64.urlsafe_b64encode(hashlib.sha256(get_settings().session_secret.encode()).digest()).decode()
    try:
        return Fernet(key.encode())
    except ValueError:
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))


def encrypt(value: str | dict) -> str:
    raw = value if isinstance(value, str) else json.dumps(value)
    return _fernet().encrypt(raw.encode()).decode()


def decrypt(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return None


def decrypt_json(token: str | None) -> dict:
    raw = decrypt(token)
    return json.loads(raw) if raw else {}


def issue_session_jwt(user_id: uuid.UUID, session_id: uuid.UUID, expires_at: datetime) -> str:
    payload = {"sub": str(user_id), "sid": str(session_id), "exp": int(expires_at.timestamp())}
    return jwt.encode(payload, get_settings().session_secret, algorithm="HS256")


def read_session_jwt(token: str) -> dict | None:
    try:
        return jwt.decode(token, get_settings().session_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def session_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=get_settings().session_ttl_days)


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().session_secret, salt="oauth-state")


def sign_state(data: dict[str, Any]) -> str:
    return _serializer().dumps(data)


def load_state(value: str, max_age: int = 600) -> dict[str, Any] | None:
    try:
        return _serializer().loads(value, max_age=max_age)
    except BadData:
        return None


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def random_token(n: int = 32) -> str:
    return secrets.token_urlsafe(n)
