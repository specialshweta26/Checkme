"""Lumio desktop app: the user's own computer as an agent target, with E2B as fallback."""

from app.desktop.client import CLIENT_DESKTOP, client_kind, prefer_local, reset_client_kind, set_client_kind

__all__ = ["CLIENT_DESKTOP", "client_kind", "prefer_local", "reset_client_kind", "set_client_kind"]
