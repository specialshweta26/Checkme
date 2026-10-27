from __future__ import annotations

import uuid
from typing import Any

from connector_core import BinaryResult, ToolError
from gmail_mcp import GmailConnector
from outlook_mcp import OutlookConnector

from app.auth.tokens import ReauthRequired, account_access_token, mcp_access_token
from app.connectors.mcp_client import call_mcp_tool
from app.models import Connector
from app.security import decrypt_json

_BUILTIN = {"gmail": GmailConnector, "outlook": OutlookConnector}


class RuntimeConnector:
    def __init__(self, row: Connector):
        self.row = row
        self.id = row.id
        self.kind = row.kind
        self.provider = row.provider
        self.name = row.alias or row.name
        self.slug = row.slug

    async def call(self, name: str, args: dict[str, Any]) -> Any:
        if self.kind == "builtin":
            if self.provider == "daytona":
                from app.connectors.daytona_box import call_daytona

                return await call_daytona(self.row, name, args)
            if self.provider in ("composio_github", "github_app"):
                from app.connectors.github import call_github

                return await call_github(self.row, name, args)
            if self.provider == "composio":
                from app.connectors.composio_box import call_composio

                return await call_composio(self.row, name, args)
            cls = _BUILTIN.get(self.provider)
            if cls is None:
                raise ToolError(f"Unknown builtin provider {self.provider}")
            account_id = self.row.account_id
            if account_id is None:
                raise ToolError("Builtin connector is missing its OAuth account")

            async def token():
                return await account_access_token(account_id)

            return await cls(token).call(name, args)

        creds = decrypt_json(self.row.credentials_enc)
        if self.row.auth_type == "oauth":
            creds["access_token"] = await mcp_access_token(self.row.id)
        if not self.row.server_url:
            raise ToolError("MCP connector has no server URL")
        try:
            return await call_mcp_tool(self.row.server_url, creds, name, args)
        except ReauthRequired as exc:
            raise ToolError(str(exc)) from exc


def is_binary(result: Any) -> bool:
    return isinstance(result, BinaryResult)
