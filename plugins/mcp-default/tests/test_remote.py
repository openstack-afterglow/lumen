"""Conformance and authority boundaries for the packaged remote MCP provider."""
from __future__ import annotations

import base64
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from lumen_mcp_default.remote import connection, create_remote_plugin, result_to_output
from lumen_plugin_api.contracts import ExecutionContext, Namespace, PluginError, PluginHost
from lumen_plugin_api.mcp import McpConnection, McpSelection, McpSnapshot, McpToolOutput
from lumen_plugin_api.testing import check_entry_point, check_lifecycle, check_manifest

USER = Namespace(user_id="alice", project_id="team")
URL = "https://mcp.example/tools"
CONFIG = {"callback_url": "https://lumen.example/callback", "allowed_return_origins": ["https://lumen.example"]}


class FakeExtensions:
    def __init__(self):
        self.server = {"id": 23, "url": URL, "is_active": True, "auth_mode": "oauth", "config_version": 3}

    async def resolve(self, kind, server_id, namespace):
        assert (kind, server_id, namespace) == ("mcp", 23, USER)
        return dict(self.server)


class FakeConnections:
    def __init__(self):
        self.row = McpConnection(
            id="conn", server_id=23, user_id=USER.user_id, project_id=USER.project_id,
            config_version=3, credential_epoch=2, status="active", token={"access_token": "secret"},
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    def oauth_configuration(self):
        return CONFIG

    async def connection(self, server_id, namespace):
        assert (server_id, namespace) == (23, USER)
        return self.row


class FakeHttp:
    def validate_url(self, url):
        assert url == URL


class FakeTool:
    name = "search"
    description = "Search notes"
    args_schema = {"type": "object", "properties": {"query": {"type": "string"}}}

    async def ainvoke(self, arguments):
        return SimpleNamespace(content=[{"type": "text", "text": arguments["query"]}])


class FakeClient:
    async def get_tools(self, *, server_name):
        assert server_name == "remote"
        return [FakeTool()]


async def ready():
    extensions, connections = FakeExtensions(), FakeConnections()
    plugin = create_remote_plugin()
    await plugin.start(PluginHost(configuration={}, extensions=extensions, mcp_connections=connections, public_http=FakeHttp()))
    plugin.transport._client = lambda server: FakeClient()
    return plugin, extensions, connections


def test_transport_rejects_invalid_destination_and_decodes_bounded_output():
    for server in ({"url": URL, "transport": "stdio"}, {"url": "http://mcp.example/tools"}):
        with pytest.raises(ValueError):
            connection(server, lambda: None)
    assert connection({"url": URL}, lambda: None)["transport"] == "streamable_http"
    result = SimpleNamespace(content=[
        {"type": "text", "text": "x" * 7000},
        {"type": "image", "base64": base64.b64encode(b"image").decode(), "mime_type": "image/png"},
    ])
    output = result_to_output(result, tool_name="search")
    assert output.text == "x" * 6000
    assert output.files[0].data == b"image"
    assert output.files[0].media_type == "image/png"
    with pytest.raises(ValueError, match="invalid embedded file"):
        result_to_output(SimpleNamespace(content=[{"type": "file", "base64": "!invalid"}]), tool_name="search")


async def test_factory_manifest_protocol_and_host_lifecycle():
    plugin = create_remote_plugin()
    assert check_manifest(plugin, expected_kind="mcp").id == "remote-mcp"
    check_entry_point("lumen-mcp-default", plugin)
    extensions, connections = FakeExtensions(), FakeConnections()
    closed = await check_lifecycle(
        create_remote_plugin,
        PluginHost(configuration={}, extensions=extensions, mcp_connections=connections, public_http=FakeHttp()),
    )
    with pytest.raises(PluginError) as excinfo:
        await closed.list_tools({"url": URL})
    assert excinfo.value.code == "plugin_unavailable"


async def test_remote_describe_bind_and_authority_changes():
    plugin, extensions, connections = await ready()
    selection = McpSelection(authority="remote", namespace=USER, server_ids=(23, 23))
    snapshots = await plugin.describe(selection)
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.credential_epoch == 2
    assert snapshot.tools[0].name == "mcp_23_search"
    assert snapshot.tools[0].input_schema["additionalProperties"] is False
    context = ExecutionContext(user_id="alice", project_id="team", run_id="run", call_id="call")
    binding = (await plugin.bind(snapshot, context))[0]
    assert (await binding.execute({"query": "find this"}, context)).model_content == "find this"
    with pytest.raises(PluginError) as failure:
        await plugin.bind(snapshot, ExecutionContext(user_id="other", project_id="team"))
    assert failure.value.code == "plugin_authority_revoked"
    extensions.server["config_version"] = 4
    with pytest.raises(PluginError) as failure:
        await plugin.revalidate(snapshot)
    assert failure.value.code == "plugin_authority_revoked"
    connections.row = connections.row.model_copy(update={"config_version": 4})
    with pytest.raises(PluginError) as failure:
        await plugin.revalidate(snapshot)
    assert failure.value.code == "plugin_configuration_changed"
    extensions.server["config_version"] = 3
    connections.row = connections.row.model_copy(update={"config_version": 3, "credential_epoch": 3})
    with pytest.raises(PluginError) as failure:
        await plugin.revalidate(snapshot)
    assert failure.value.code == "plugin_configuration_changed"
    connections.row = connections.row.model_copy(update={"credential_epoch": 2})
    extensions.server["effect_overrides"] = {"search": "external_mutation"}
    with pytest.raises(PluginError) as failure:
        await plugin.revalidate(snapshot)
    assert failure.value.code == "plugin_configuration_changed"
    connections.row = None
    with pytest.raises(PluginError) as failure:
        await plugin.revalidate(snapshot)
    assert failure.value.code == "plugin_authority_revoked"
    await plugin.close()


async def test_remote_dispatches_via_transport_and_rejects_stale_binding():
    plugin, extensions, connections = await ready()
    snapshot = (await plugin.describe(McpSelection(authority="remote", namespace=USER, server_ids=(23,))))[0]
    context = ExecutionContext(user_id="alice", project_id="team")
    binding = (await plugin.bind(snapshot, context))[0]
    extensions.server["config_version"] += 1
    connections.row = connections.row.model_copy(update={"config_version": 4})
    with pytest.raises(PluginError) as failure:
        await binding.execute({"query": "stale"}, context)
    assert failure.value.code == "plugin_configuration_changed"


async def test_remote_tool_without_optional_description_uses_method_name():
    plugin, _extensions, _connections = await ready()

    class NoDescription(FakeTool):
        description = None

    class NoDescriptionClient(FakeClient):
        async def get_tools(self, *, server_name):
            assert server_name == "remote"
            return [NoDescription()]

    plugin.transport._client = lambda _server: NoDescriptionClient()
    snapshot = (await plugin.describe(McpSelection(authority="remote", namespace=USER, server_ids=(23,))))[0]
    assert snapshot.tools[0].description == "search"


async def test_remote_punctuated_names_are_provider_safe_and_dispatch_original_methods(monkeypatch):
    plugin, extensions, _connections = await ready()
    originals = ["get-weather", "get_weather", "search.docs", "special:run", "really_long_" + "x" * 160]
    extensions.server["effect_overrides"] = {"get-weather": "external_mutation"}

    async def list_tools(_server):
        return [{"name": name, "description": name, "input_schema": {"type": "object", "properties": {},
                "additionalProperties": False}} for name in originals]

    called = []

    async def call_tool(_server, method, arguments):
        called.append((method, arguments))
        return McpToolOutput(text=method, files=())

    monkeypatch.setattr(plugin, "list_tools", list_tools)
    monkeypatch.setattr(plugin, "call_tool", call_tool)
    snapshot = (await plugin.describe(McpSelection(authority="remote", namespace=USER, server_ids=(23,))))[0]
    restored = McpSnapshot.model_validate(snapshot.model_dump(mode="json"))
    context = ExecutionContext(user_id="alice", project_id="team")
    bindings = await plugin.bind(restored, context)
    names = [binding.definition.name for binding in bindings]
    assert len(set(names)) == len(originals)
    assert all(len(name) <= 128 and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name) for name in names)
    assert bindings[0].definition.effect == "external_mutation"
    for original, binding in zip(originals, bindings, strict=True):
        assert restored.remote_tool_names[binding.definition.name] == original
        assert (await binding.execute({"query": original}, context)).model_content == original
    assert called == [(original, {"query": original}) for original in originals]

    changed = restored.model_copy(update={"remote_tool_names": {names[0]: "other-name"}})
    with pytest.raises(PluginError, match="snapshot tool names changed"):
        await plugin.bind(changed, context)
