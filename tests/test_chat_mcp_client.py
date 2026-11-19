"""Core MCP client lane: delegates to the started remote-mcp plugin via the registry.

Transport hardening, streamable-HTTP validation, and result projection are owned by
the plugins/mcp-default package and exercised by its own test suite. Core only owns
the fail-closed discovery and typed dispatch-error propagation behavior below.
"""

from __future__ import annotations

import pytest
from lumen_plugin_api.contracts import ExecutionContext, PluginError

from lumen.services import mcp_client

CONTEXT = ExecutionContext(user_id="alice", project_id="team", run_id="run-1")

class _FakeProvider:
    def __init__(self, *, tools=None, output=None, raise_list=False, raise_call=False):
        self._tools = tools or []
        self._output = output
        self._raise_list = raise_list
        self._raise_call = raise_call
        self.list_calls: list[dict] = []
        self.call_calls: list[tuple] = []
        self.contexts = []
        self.epochs = []

    async def list_tools(self, server, *, context=None):
        self.list_calls.append(server)
        self.contexts.append(context)
        if self._raise_list:
            raise RuntimeError("boom")
        return self._tools

    async def call_tool(self, server, tool_name, arguments, *, context, expected_credential_epoch):
        self.call_calls.append((server, tool_name, arguments))
        self.contexts.append(context)
        self.epochs.append(expected_credential_epoch)
        if self._raise_call:
            raise self._raise_call if isinstance(self._raise_call, Exception) else RuntimeError("boom")
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

    result = await mcp_client.list_tools(server, context=CONTEXT)

    assert result == [{"name": "search", "description": "", "input_schema": {}}]
    assert provider.list_calls == [server]
    assert provider.contexts == [CONTEXT]


async def test_list_tools_fails_closed_to_empty_list_on_plugin_error(_provider):
    provider = _provider(_FakeProvider(raise_list=True))

    assert await mcp_client.list_tools({"id": 1}) == []
    assert provider.list_calls == [{"id": 1}]


async def test_call_tool_returns_text_when_no_generated_files(_provider):
    provider = _provider(
        _FakeProvider(output=mcp_client.McpToolOutput(text="result", files=()))
    )

    result = await mcp_client.call_tool({"id": 1}, "search", {"query": "x"}, context=CONTEXT, expected_credential_epoch=None)

    assert result == "result"
    assert provider.call_calls == [({"id": 1}, "search", {"query": "x"})]
    assert provider.contexts == [CONTEXT]


async def test_call_tool_returns_output_object_when_files_are_present(_provider):
    output = mcp_client.McpToolOutput(
        text="Created chart.",
        files=(mcp_client.McpGeneratedFile(name="chart.png", media_type="image/png", data=b"\x89PNG"),),
    )
    _provider(_FakeProvider(output=output))

    result = await mcp_client.call_tool({"id": 1}, "render_chart", {}, context=CONTEXT, expected_credential_epoch=None)

    assert result is output


@pytest.mark.parametrize("error", [PluginError("plugin_authority_revoked"), PluginError("plugin_configuration_changed"), RuntimeError("boom")])
async def test_call_tool_preserves_typed_failures_for_execution_boundary(_provider, error):
    provider = _provider(_FakeProvider(raise_call=error))
    with pytest.raises(type(error)) as failure:
        await mcp_client.call_tool({"id": 1}, "search", {}, context=CONTEXT, expected_credential_epoch=2)
    assert failure.value is error
    assert provider.call_calls == [({"id": 1}, "search", {})]
    assert provider.epochs == [2]
