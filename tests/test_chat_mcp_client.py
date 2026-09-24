"""Core MCP client lane: delegates to the started remote-mcp plugin via the registry.

Transport hardening, streamable-HTTP validation, and result projection are owned by
the plugins/mcp-default package and exercised by its own test suite. Core only owns
the fail-closed delegation and exception-to-fallback-value behavior below.
"""

from __future__ import annotations

import pytest

from lumen.services import mcp_client


class _FakeProvider:
    def __init__(self, *, tools=None, output=None, raise_list=False, raise_call=False):
        self._tools = tools or []
        self._output = output
        self._raise_list = raise_list
        self._raise_call = raise_call
        self.list_calls: list[dict] = []
        self.call_calls: list[tuple] = []

    async def list_tools(self, server):
        self.list_calls.append(server)
        if self._raise_list:
            raise RuntimeError("boom")
        return self._tools

    async def call_tool(self, server, tool_name, arguments):
        self.call_calls.append((server, tool_name, arguments))
        if self._raise_call:
            raise RuntimeError("boom")
        return self._output


@pytest.fixture(autouse=True)
def _provider(monkeypatch):
    holder: dict[str, _FakeProvider] = {}

    def factory():
        return holder["provider"]

    monkeypatch.setattr(mcp_client, "_provider", factory)

    def _set(provider: _FakeProvider) -> _FakeProvider:
        holder["provider"] = provider
        return provider

    yield _set


async def test_list_tools_delegates_to_started_plugin(_provider):
    server = {"id": 1, "name": "srv"}
    provider = _provider(_FakeProvider(tools=[{"name": "search", "description": "", "input_schema": {}}]))

    result = await mcp_client.list_tools(server)

    assert result == [{"name": "search", "description": "", "input_schema": {}}]
    assert provider.list_calls == [server]


async def test_list_tools_fails_closed_to_empty_list_on_plugin_error(_provider):
    provider = _provider(_FakeProvider(raise_list=True))

    assert await mcp_client.list_tools({"id": 1}) == []
    assert provider.list_calls == [{"id": 1}]


async def test_call_tool_returns_text_when_no_generated_files(_provider):
    provider = _provider(
        _FakeProvider(output=mcp_client.McpToolOutput(text="result", files=()))
    )

    result = await mcp_client.call_tool({"id": 1}, "search", {"query": "x"})

    assert result == "result"
    assert provider.call_calls == [({"id": 1}, "search", {"query": "x"})]


async def test_call_tool_returns_output_object_when_files_are_present(_provider):
    output = mcp_client.McpToolOutput(
        text="Created chart.",
        files=(mcp_client.McpGeneratedFile(name="chart.png", media_type="image/png", data=b"\x89PNG"),),
    )
    _provider(_FakeProvider(output=output))

    result = await mcp_client.call_tool({"id": 1}, "render_chart", {})

    assert result is output


async def test_call_tool_fails_closed_to_safe_message_on_plugin_error(_provider):
    provider = _provider(_FakeProvider(raise_call=True))

    result = await mcp_client.call_tool({"id": 1}, "search", {})

    assert result == "MCP 도구 실행 중 오류가 발생했습니다."
    assert provider.call_calls == [({"id": 1}, "search", {})]
