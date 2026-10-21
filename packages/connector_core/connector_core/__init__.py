from connector_core.spec import (
    BaseConnector,
    BinaryResult,
    ConnectorAuthError,
    ToolError,
    ToolSpec,
    TokenProvider,
)
from connector_core.mail_tools import MAIL_CALENDAR_TOOLS, canonical_mail_folder, folder_from_query

__all__ = [
    "BaseConnector",
    "BinaryResult",
    "ConnectorAuthError",
    "MAIL_CALENDAR_TOOLS",
    "canonical_mail_folder",
    "folder_from_query",
    "TokenProvider",
    "ToolError",
    "ToolSpec",
]
