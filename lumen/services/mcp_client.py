"""Caller-facing remote MCP lane backed by the independently packaged plugin.

Core never imports the plugin package directly; it resolves the started
``remote-mcp`` plugin through the registry and calls its public protocol.
"""
from __future__ import annotations

import logging
from typing import Any

from lumen_plugin_api.mcp import McpGeneratedFile, McpRemoteProvider, McpToolOutput

from lumen.plugins.registry import get_plugin

logger = logging.getLogger(__name__)

__all__ = ["McpGeneratedFile", "McpToolOutput", "list_tools", "call_tool"]


def _provider() -> McpRemoteProvider:
    return get_plugin("mcp", "remote-mcp")


async def list_tools(server: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        return await _provider().list_tools(server)
    except Exception:
        logger.warning("MCP list_tools failed name=%s", server.get("name"), exc_info=True)
        return []


async def call_tool(server: dict[str, Any], tool_name: str, args: dict[str, Any]) -> str | McpToolOutput:
    try:
        output = await _provider().call_tool(server, tool_name, args)
        return output if output.files else output.text
    except Exception:
        logger.warning("MCP call_tool failed name=%s tool=%s", server.get("name"), tool_name, exc_info=True)
        return "MCP 도구 실행 중 오류가 발생했습니다."
