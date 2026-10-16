"""Caller-facing remote MCP lane backed by the independently packaged plugin.

Core never imports the plugin package directly; it resolves the started
``remote-mcp`` plugin through the registry and calls its public protocol.
"""
from __future__ import annotations

import logging
from typing import Any

from lumen_plugin_api.contracts import ExecutionContext
from lumen_plugin_api.mcp import McpGeneratedFile, McpRemoteProvider, McpToolOutput

from lumen.plugins.registry import get_plugin

logger = logging.getLogger(__name__)

__all__ = ["McpGeneratedFile", "McpToolOutput", "list_tools", "call_tool"]


def _provider() -> McpRemoteProvider:
    return get_plugin("mcp", "remote-mcp")


async def list_tools(server: dict[str, Any], *, context: ExecutionContext | None = None) -> list[dict[str, Any]]:
    try:
        return await _provider().list_tools(server, context=context)
    except Exception:
        logger.warning("MCP list_tools failed name=%s", server.get("name"), exc_info=True)
        return []


async def call_tool(server: dict[str, Any], tool_name: str, args: dict[str, Any], *, context: ExecutionContext, expected_credential_epoch: int | None) -> str | McpToolOutput:
    output = await _provider().call_tool(server, tool_name, args, context=context, expected_credential_epoch=expected_credential_epoch)
    return output if output.files else output.text
