"""Expose any BaseConnector as a standalone stdio MCP server (for Cursor, Claude, etc.)."""

from __future__ import annotations

import base64
import json
from typing import Any

from connector_core.spec import BaseConnector, BinaryResult, ToolError


def _json_default(value: Any) -> Any:
    if isinstance(value, bytes):
        return base64.b64encode(value).decode()
    return str(value)


async def serve_stdio(connector: BaseConnector, name: str) -> None:
    import mcp.types as types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    server = Server(name)
    specs = connector.tools()

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        tools = []
        for s in specs:
            schema = json.loads(json.dumps(s.input_schema))
            if s.name == "bulk_delete":
                schema["properties"]["confirm"] = {
                    "type": "boolean",
                    "description": "false = preview only (default). true = actually delete.",
                }
            tools.append(
                types.Tool(
                    name=s.name,
                    description=s.description,
                    inputSchema=schema,
                    annotations=types.ToolAnnotations(readOnlyHint=s.read_only, destructiveHint=s.destructive),
                )
            )
        return tools

    @server.call_tool()
    async def call_tool(tool: str, arguments: dict) -> list:
        args = dict(arguments or {})
        if tool == "bulk_delete":
            args["dry_run"] = not bool(args.pop("confirm", False))
        try:
            result = await connector.call(tool, args)
        except ToolError as exc:
            return [types.TextContent(type="text", text=f"Error: {exc}")]
        if isinstance(result, BinaryResult):
            return [
                types.EmbeddedResource(
                    type="resource",
                    resource=types.BlobResourceContents(
                        uri=f"attachment://{result.filename}",
                        mimeType=result.content_type,
                        blob=base64.b64encode(result.data).decode(),
                    ),
                )
            ]
        return [types.TextContent(type="text", text=json.dumps(result, default=_json_default))]

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
