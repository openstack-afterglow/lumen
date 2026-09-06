"""Hardened LangChain MCP adapter for remote Streamable HTTP data sources.

Only HTTPS Streamable HTTP connections are admitted. The adapter receives a
DNS-pinned HTTPX factory, so neither its temporary sessions nor MCP redirects
can reach private networks. Stdio, commands, and legacy SSE are never accepted.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import mimetypes
import re
from collections.abc import Iterable
from dataclasses import dataclass
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
_MAX_GENERATED_FILES = 20
_MAX_GENERATED_FILE_BYTES = 5 * 1024 * 1024
_SAFE_FILE_STEM = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class McpGeneratedFile:
    """Bounded embedded file returned by a remote MCP tool."""

    name: str
    media_type: str
    data: bytes


@dataclass(frozen=True)
class McpToolOutput:
    """Text plus embedded files projected from a LangChain MCP result."""

    text: str
    files: tuple[McpGeneratedFile, ...]


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


def _result_content(result: Any) -> Any:
    if isinstance(result, tuple):
        result = result[0]
    return getattr(result, "content", result)


def _block_value(block: object, key: str) -> object:
    return block.get(key) if isinstance(block, dict) else getattr(block, key, None)


def _generated_file_name(tool_name: str, index: int, media_type: str, block: object) -> str:
    supplied = _block_value(block, "name") or _block_value(block, "filename")
    if isinstance(supplied, str) and supplied.strip():
        return supplied.strip()[:255]
    stem = _SAFE_FILE_STEM.sub("-", tool_name).strip(".-") or "mcp-output"
    extension = mimetypes.guess_extension(media_type, strict=False) or ".bin"
    return f"{stem[: 240 - len(extension)]}-{index}{extension}"


def _result_to_output(result: Any, *, tool_name: str) -> McpToolOutput:
    """Extract bounded text and embedded base64 files from a LangChain MCP result."""
    content = _result_content(result)
    if isinstance(content, str):
        return McpToolOutput(text=content[:_MAX_RESULT_CHARS], files=())
    if not isinstance(content, Iterable) or isinstance(content, (bytes, bytearray, dict)):
        return McpToolOutput(text=str(content)[:_MAX_RESULT_CHARS], files=())
    text_parts: list[str] = []
    files: list[McpGeneratedFile] = []
    total_file_bytes = 0
    for block in content:
        if isinstance(block, str):
            text_parts.append(block)
            continue
        text = _block_value(block, "text")
        if isinstance(text, str):
            text_parts.append(text)
        block_type = _block_value(block, "type")
        encoded = _block_value(block, "base64")
        if block_type not in {"image", "file"} or not isinstance(encoded, str):
            continue
        if len(files) >= _MAX_GENERATED_FILES:
            raise ValueError("MCP tool returned too many embedded files")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("MCP tool returned invalid embedded file data") from exc
        total_file_bytes += len(data)
        if not data or len(data) > _MAX_GENERATED_FILE_BYTES or total_file_bytes > _MAX_GENERATED_FILE_BYTES:
            raise ValueError("MCP tool returned oversized embedded file data")
        media_type = _block_value(block, "mime_type")
        normalized_media_type = (
            media_type.strip().lower() if isinstance(media_type, str) else "application/octet-stream"
        )
        files.append(
            McpGeneratedFile(
                name=_generated_file_name(tool_name, len(files) + 1, normalized_media_type, block),
                media_type=normalized_media_type,
                data=data,
            )
        )
    return McpToolOutput(text="\n".join(text_parts)[:_MAX_RESULT_CHARS], files=tuple(files))


def _result_to_text(result: Any) -> str:
    """Extract bounded text from LangChain's text or structured MCP result."""
    return _result_to_output(result, tool_name="mcp-output").text


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


async def call_tool(server: dict, tool_name: str, args: dict) -> str | McpToolOutput:
    """Invoke one LangChain-adapted MCP tool using a fresh hardened session."""

    async def _run() -> str | McpToolOutput:
        tools = await _client(server).get_tools(server_name=_SERVER_NAME)
        tool = next((candidate for candidate in tools if candidate.name == tool_name), None)
        if tool is None:
            raise ValueError("MCP tool is unavailable")
        output = _result_to_output(
            await tool.ainvoke(args if isinstance(args, dict) else {}),
            tool_name=tool_name,
        )
        return output if output.files else output.text

    try:
        return await asyncio.wait_for(_run(), timeout=_TIMEOUT_SECONDS)
    except Exception:
        logger.warning("MCP call_tool failed name=%s tool=%s", server.get("name"), tool_name, exc_info=True)
        return "MCP 도구 실행 중 오류가 발생했습니다."
