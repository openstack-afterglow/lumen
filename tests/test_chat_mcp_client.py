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


def test_result_projection_preserves_embedded_mcp_files():
    output = mcp_client._result_to_output(
        (
            [
                {"type": "text", "text": "Created the report."},
                {
                    "type": "file",
                    "base64": "bmFtZSx2YWx1ZQpsYXRlbmN5LDEyCg==",
                    "mime_type": "text/csv",
                },
            ],
            {"structured_content": {"rows": 1}},
        ),
        tool_name="report_data",
    )

    assert output.text == "Created the report."
    assert output.files == (
        mcp_client.McpGeneratedFile(
            name="report_data-1.csv",
            media_type="text/csv",
            data=b"name,value\nlatency,12\n",
        ),
    )


def test_result_projection_rejects_invalid_embedded_file_data():
    with pytest.raises(ValueError, match="invalid embedded file data"):
        mcp_client._result_to_output(
            [{"type": "file", "base64": "not base64!", "mime_type": "text/plain"}],
            tool_name="report",
        )


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


async def test_langchain_adapter_returns_embedded_files_to_the_runtime(monkeypatch):
    class Tool:
        name = "render_chart"
        description = "Render a chart"
        args_schema = {"type": "object", "properties": {}}

        async def ainvoke(self, _args):
            return [
                {"type": "text", "text": "Created chart."},
                {"type": "image", "base64": "iVBORw0KGgo=", "mime_type": "image/png"},
            ]

    class Client:
        async def get_tools(self, *, server_name):
            assert server_name == "remote"
            return [Tool()]

    monkeypatch.setattr(mcp_client, "_client", lambda _server: Client())
    result = await mcp_client.call_tool(
        {"name": "public", "transport": "http", "url": "https://mcp.example"},
        "render_chart",
        {},
    )

    assert isinstance(result, mcp_client.McpToolOutput)
    assert result.text == "Created chart."
    assert result.files[0].name == "render_chart-1.png"
    assert result.files[0].data == b"\x89PNG\r\n\x1a\n"
