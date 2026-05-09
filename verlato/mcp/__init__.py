from __future__ import annotations

import importlib
from typing import Any

from verlato.mcp.service import MCPService, ReleasePreviewRequest

__all__ = [
    "MCPService",
    "ReleasePreviewRequest",
    "create_fastmcp_server",
    "list_prompt_metadata",
    "list_resource_metadata",
    "list_tool_metadata",
    "run_mcp_stdio",
]


def __getattr__(name: str) -> Any:
    if name in {
        "create_fastmcp_server",
        "list_prompt_metadata",
        "list_resource_metadata",
        "list_tool_metadata",
        "run_mcp_stdio",
    }:
        server = importlib.import_module("verlato.mcp.server")
        return getattr(server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
