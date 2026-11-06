"""Which client is calling (web vs desktop). Web always uses the cloud computer."""

from __future__ import annotations

from contextvars import ContextVar, Token

CLIENT_WEB = "web"
CLIENT_DESKTOP = "desktop"
_CLIENT: ContextVar[str] = ContextVar("lumio_client", default=CLIENT_WEB)


def set_client_kind(kind: str) -> Token:
    value = (kind or "").strip().lower()
    if value not in {CLIENT_WEB, CLIENT_DESKTOP}:
        value = CLIENT_WEB
    return _CLIENT.set(value)


def reset_client_kind(token: Token) -> None:
    _CLIENT.reset(token)


def client_kind() -> str:
    return _CLIENT.get()


def prefer_local() -> bool:
    """Desktop app uses the laptop when it is online; the website always stays on E2B."""
    return client_kind() == CLIENT_DESKTOP
