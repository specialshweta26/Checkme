from __future__ import annotations

import inspect
from abc import ABC
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

TokenProvider = Callable[[], Awaitable[str]]


class ToolError(Exception):
    """A tool failed in a way the model can read and react to."""


class ConnectorAuthError(ToolError):
    """The provider rejected the credentials; the connector needs reconnecting."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    group: str
    read_only: bool = True
    destructive: bool = False
    requires: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "group": self.group,
            "read_only": self.read_only,
            "destructive": self.destructive,
            "requires": list(self.requires),
            "produces": list(self.produces),
            "tags": list(self.tags),
        }


@dataclass
class BinaryResult:
    filename: str
    content_type: str
    data: bytes = field(repr=False)


class BaseConnector(ABC):
    """In-process connector. Tool `foo` is implemented by `async def tool_foo(self, **kwargs)`."""

    provider: str = ""
    tool_specs: list[ToolSpec] = []

    def __init__(self, token_provider: TokenProvider):
        self._token = token_provider

    def tools(self) -> list[ToolSpec]:
        return list(self.tool_specs)

    def spec(self, name: str) -> ToolSpec | None:
        return next((t for t in self.tool_specs if t.name == name), None)

    async def call(self, name: str, args: dict[str, Any]) -> Any:
        spec = self.spec(name)
        fn = getattr(self, f"tool_{name}", None)
        if spec is None or fn is None:
            raise ToolError(f"Unknown tool '{name}' for {self.provider}")
        params = inspect.signature(fn).parameters
        accepted = {
            k: v for k, v in (args or {}).items() if k in params and not k.startswith("_") and v is not None
        }
        missing = [
            p.name
            for p in params.values()
            if p.default is inspect.Parameter.empty and p.name not in accepted
        ]
        if missing:
            raise ToolError(f"Missing required argument(s): {', '.join(missing)}")
        return await fn(**accepted)
