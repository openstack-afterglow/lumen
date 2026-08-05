"""Hardened LangChain MCP adapter for remote Streamable HTTP data sources.

Only HTTPS Streamable HTTP connections are admitted. The adapter receives a
DNS-pinned HTTPX factory, so neither its temporary sessions nor MCP redirects
can reach private networks. Stdio, commands, and legacy SSE are never accepted.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

import httpx

from lumen.services import ssrf

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 15
_MAX_RESULT_CHARS = 6000
_MAX_TOOLS_PER_SERVER = 40
_MAX_RESPONSE_BYTES = 1024 * 1024
_SERVER_NAME = "remote"


def _safe_http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    """Create the only HTTP client the remote MCP adapter may use."""
    request_headers = dict(headers or {})
    request_headers["Accept-Encoding"] = "identity"
    return httpx.AsyncClient(
        transport=ssrf.SafeAsyncTransport(max_response_bytes=_MAX_RESPONSE_BYTES),
        headers=request_headers,
        timeout=timeout or httpx.Timeout(_TIMEOUT_SECONDS),
        follow_redirects=False,
        trust_env=False,
        auth=auth,
    )


def _connection(server: dict) -> dict[str, Any]:
    """Build an adapter connection only after enforcing the remote transport policy."""
    transport = (server.get("transport") or "http").lower()
    if transport not in ("http", "streamable_http", "streamable-http"):
        raise ValueError("streamable HTTP MCP transport is required")
    url = server.get("url") or ""
    if urlsplit(url).scheme.lower() != "https":
        raise ValueError("MCP URL must use HTTPS")
    headers = server.get("headers") or {}
    if not isinstance(headers, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in headers.items()
    ):
        raise ValueError("MCP headers must be string pairs")
    return {
        "transport": "streamable_http",
        "url": url,
        "headers": headers,
        "timeout": _TIMEOUT_SECONDS,
        "httpx_client_factory": _safe_http_client,
    }


def _client(server: dict):
    """Create a stateless LangChain client for exactly one remote data source."""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    return MultiServerMCPClient({_SERVER_NAME: _connection(server)}, handle_tool_errors=False)


def _input_schema(tool: Any) -> dict[str, Any]:
    schema = getattr(tool, "args_schema", None)
    if isinstance(schema, dict):
        return schema
    model_json_schema = getattr(schema, "model_json_schema", None)
    if callable(model_json_schema):
        result = model_json_schema()
        if isinstance(result, dict):
            return result
    return {"type": "object", "properties": {}}


def _result_to_text(result: Any) -> str:
    """Extract bounded text from LangChain's text or structured MCP result."""
    if isinstance(result, tuple):
        result = result[0]
    content = getattr(result, "content", result)
    if isinstance(content, str):
        return content[:_MAX_RESULT_CHARS]
    if isinstance(content, Iterable) and not isinstance(content, (bytes, bytearray, dict)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(getattr(block, "text", None), str):
                parts.append(block.text)
        return "\n".join(parts)[:_MAX_RESULT_CHARS]
    return str(content)[:_MAX_RESULT_CHARS]


async def list_tools(server: dict) -> list[dict]:
    """List one server's LangChain-adapted MCP tools without retaining a session."""

    async def _run() -> list[dict]:
        tools = await _client(server).get_tools(server_name=_SERVER_NAME)
        return [
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": _input_schema(tool),
            }
            for tool in tools[:_MAX_TOOLS_PER_SERVER]
        ]

    try:
        return await asyncio.wait_for(_run(), timeout=_TIMEOUT_SECONDS)
    except Exception:
        logger.warning("MCP list_tools failed name=%s", server.get("name"), exc_info=True)
        return []


async def call_tool(server: dict, tool_name: str, args: dict) -> str:
    """Invoke one LangChain-adapted MCP tool using a fresh hardened session."""

    async def _run() -> str:
        tools = await _client(server).get_tools(server_name=_SERVER_NAME)
        tool = next((candidate for candidate in tools if candidate.name == tool_name), None)
        if tool is None:
            raise ValueError("MCP tool is unavailable")
        return _result_to_text(await tool.ainvoke(args if isinstance(args, dict) else {}))

    try:
        return await asyncio.wait_for(_run(), timeout=_TIMEOUT_SECONDS)
    except Exception:
        logger.warning("MCP call_tool failed name=%s tool=%s", server.get("name"), tool_name, exc_info=True)
        return "MCP 도구 실행 중 오류가 발생했습니다."
