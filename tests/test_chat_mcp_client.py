"""MCP streamable-HTTP transport hardening tests."""

from __future__ import annotations

import httpx
import pytest

from lumen.services import mcp_client, ssrf


def test_connection_rejects_legacy_sse_and_non_https():
    with pytest.raises(ValueError, match="streamable HTTP"):
        mcp_client._connection({"transport": "sse", "url": "https://mcp.example"})
    with pytest.raises(ValueError, match="HTTPS"):
        mcp_client._connection({"transport": "http", "url": "http://mcp.example"})


def test_connection_passes_hardened_factory_to_langchain_adapter():
    connection = mcp_client._connection(
        {"transport": "streamable_http", "url": "https://mcp.example/api", "headers": {"Authorization": "Bearer x"}}
    )

    assert connection == {
        "transport": "streamable_http",
        "url": "https://mcp.example/api",
        "headers": {"Authorization": "Bearer x"},
        "timeout": mcp_client._TIMEOUT_SECONDS,
        "httpx_client_factory": mcp_client._safe_http_client,
    }


def test_result_projection_is_bounded():
    class TextBlock:
        text = "x" * (mcp_client._MAX_RESULT_CHARS + 1)

    assert mcp_client._result_to_text([TextBlock()]) == "x" * mcp_client._MAX_RESULT_CHARS


async def test_mcp_http_factory_uses_pinned_transport_and_identity_encoding():
    client = mcp_client._safe_http_client({"Accept-Encoding": "gzip", "Authorization": "Bearer x"})
    try:
        assert isinstance(client._transport, ssrf.SafeAsyncTransport)
        assert client.headers["accept-encoding"] == "identity"
        assert client.follow_redirects is False
        assert client.trust_env is False
        assert client.timeout == httpx.Timeout(mcp_client._TIMEOUT_SECONDS)
    finally:
        await client.aclose()


async def test_langchain_adapter_lists_and_invokes_stateless_tools(monkeypatch):
    class ArgsSchema:
        @staticmethod
        def model_json_schema():
            return {"type": "object", "properties": {"query": {"type": "string"}}}

    class Tool:
        name = "search"
        description = "Search the remote index"
        args_schema = ArgsSchema

        async def ainvoke(self, args):
            assert args == {"query": "Afterglow"}
            return [{"text": "result"}]

    class Client:
        async def get_tools(self, *, server_name):
            assert server_name == "remote"
            return [Tool()]

    monkeypatch.setattr(mcp_client, "_client", lambda _server: Client())
    server = {"name": "public", "transport": "http", "url": "https://mcp.example"}

    assert await mcp_client.list_tools(server) == [
        {
            "name": "search",
            "description": "Search the remote index",
            "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
        }
    ]
    assert await mcp_client.call_tool(server, "search", {"query": "Afterglow"}) == "result"
