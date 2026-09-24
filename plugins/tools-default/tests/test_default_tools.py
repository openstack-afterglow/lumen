"""Default tools conformance against capability-only fake hosts (no Lumen imports)."""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest
from lumen_plugin_api.contracts import ExecutionContext, PluginError, PluginHost, PluginIdentity
from lumen_plugin_api.testing import check_lifecycle, check_manifest, check_tool_provider_rejects_foreign_identity
from lumen_plugin_api.tools import ToolSpec, ToolTextPart
from lumen_tools_default import create_plugin

IDENTITY = PluginIdentity(plugin_id="default-tools", version="0.1.0", config_fingerprint="0" * 64)
OWNER = ExecutionContext(user_id="owner", project_id="p", run_id="r")
CUSTOM_ROW = {
    "id": 7, "name": "lookup", "description": "Look up a public URL", "url": "https://api.example/x",
    "method": "GET", "params_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    "timeout_seconds": 5, "effect": "read", "config_version": 1, "load_policy": "preloaded", "is_active": True,
}


def spec(key, configuration=None):
    return ToolSpec(binding_id="conformance", export_key=key, identity=IDENTITY, configuration=configuration or {})


@dataclass
class Conversations:
    owner: str = "owner"
    calls: list = field(default_factory=list)

    async def list(self, context, *, limit=20):
        self.calls.append((context, limit))
        return [{"id": "c1", "title": "Mine", "model_name": "m"}] if context.user_id == self.owner else []

    async def read(self, conversation_id, context):
        self.calls.append((conversation_id, context))
        if context.user_id != self.owner:
            raise PermissionError("foreign title must not leak")
        if conversation_id == "missing":
            raise LookupError("missing")
        return {"title": "Mine", "model_name": "m", "message_count": 2}


@dataclass
class Extensions:
    row: dict
    namespaces: list = field(default_factory=list)

    async def resolve(self, kind, identifier, namespace):
        self.namespaces.append(namespace)
        if kind != "tool" or identifier != self.row["id"]:
            raise PluginError("plugin_authority_revoked")
        return self.row


class StreamResponse:
    status_code = 200
    headers = {"content-encoding": "identity"}
    encoding = "utf-8"

    async def aiter_bytes(self):
        yield b'{"answer":"ok"}'


class StreamClient:
    def __init__(self, requests):
        self.requests = requests

    @asynccontextmanager
    async def stream(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        yield StreamResponse()


class Http:
    def __init__(self):
        self.requests = []
        self.stream_requests = []
        self.responses = {}

    async def request(self, method, url, **kwargs):
        self.requests.append((method, url))
        return self.responses.get(url, (200, {"content-type": "text/html"}, b"<html><title>Docs</title><nav>skip</nav><p>Helpful text</p></html>"))

    @asynccontextmanager
    async def client(self, **kwargs):
        yield StreamClient(self.stream_requests)

    def validate_url(self, url):
        return None


@dataclass
class Advisor:
    calls: list = field(default_factory=list)

    async def invoke(self, arguments, context):
        from lumen_plugin_api.tools import ToolExecutionResult

        self.calls.append((arguments, context))
        return ToolExecutionResult(status="completed", model_content="provider answer", display=[ToolTextPart(text="provider answer")], usage_components={"components": [{"kind": "provider"}]})


@pytest.mark.asyncio
async def test_manifest_lifecycle_and_foreign_identity():
    plugin = create_plugin()
    manifest = check_manifest(plugin, expected_kind="tools")
    assert {export.key for export in manifest.exports} == {
        "list_my_conversations", "get_conversation_detail", "managed_web_search", "managed_web_fetch", "managed_advisor"
    }
    await check_lifecycle(create_plugin, PluginHost(configuration={}))
    await check_tool_provider_rejects_foreign_identity(
        plugin, spec("list_my_conversations"), OWNER, PluginHost(configuration={}, conversations=Conversations())
    )


@pytest.mark.asyncio
async def test_builtin_scope_cannot_be_overridden_by_model_arguments():
    conversations = Conversations()
    plugin = create_plugin()
    host = PluginHost(configuration={}, conversations=conversations)
    binding = await plugin.bind(spec("list_my_conversations"), OWNER, host)
    result = await binding.execute({"user_id": "attacker", "project_id": "foreign"}, OWNER)
    assert result.status == "completed" and "Mine" in result.model_content
    assert conversations.calls == [(OWNER, 20)]
    assert all(isinstance(part, ToolTextPart) for part in result.display)
    with pytest.raises(PluginError, match="context changed") as exc:
        await binding.execute({}, ExecutionContext(user_id="other", project_id="p", run_id="r"))
    assert exc.value.code == "plugin_authority_revoked"
    assert conversations.calls == [(OWNER, 20)]

    detail = await plugin.bind(spec("get_conversation_detail"), OWNER, host)
    missing = await detail.execute({"conversation_id": "missing"}, OWNER)
    assert "찾을 수 없습니다" in missing.model_content
    owned = await detail.execute({"conversation_id": "c1", "user_id": "attacker"}, OWNER)
    assert "Mine" in owned.model_content and "2" in owned.model_content


@pytest.mark.asyncio
async def test_custom_http_posts_json_and_rejects_changed_row_at_bind():
    row = {**CUSTOM_ROW, "method": "POST"}
    store, http = Extensions(row), Http()
    plugin = create_plugin()
    binding = await plugin.bind(spec("custom_http", row), OWNER, PluginHost(configuration={}, extensions=store, public_http=http))
    result = await binding.execute({"query": "term"}, OWNER)
    assert result.status == "completed"
    assert http.stream_requests == [("POST", "https://api.example/x", {"json": {"query": "term"}})]
    store.row = {**row, "is_active": False}
    with pytest.raises(PluginError) as exc:
        await plugin.bind(spec("custom_http", row), OWNER, PluginHost(configuration={}, extensions=store, public_http=http))
    assert exc.value.code == "plugin_configuration_changed"


@pytest.mark.asyncio
async def test_custom_http_executes_real_host_stream_and_rechecks_namespace():
    row = dict(CUSTOM_ROW)
    store, http = Extensions(row), Http()
    binding = await create_plugin().bind(spec("custom_http", row), OWNER, PluginHost(configuration={}, extensions=store, public_http=http))
    assert binding.definition.source == "custom_http"
    assert binding.definition.name.startswith("custom__7__lookup_")
    result = await binding.execute({"q": "hi"}, OWNER)
    assert result.status == "completed" and result.model_content == '[200] {"answer":"ok"}'
    assert result.display == [ToolTextPart(text=result.model_content)]
    assert http.stream_requests == [("GET", "https://api.example/x", {"params": {"q": "hi"}})]
    assert [(ns.user_id, ns.project_id) for ns in store.namespaces] == [("owner", "p"), ("owner", "p")]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,expected", [
    (ValueError("blocked destination"), "허용되지 않은"),
    (RuntimeError("connection refused"), "오류"),
])
async def test_custom_http_host_failures_are_safe(failure, expected):
    class FailingHttp(Http):
        @asynccontextmanager
        async def client(self, **kwargs):
            class Client:
                @asynccontextmanager
                async def stream(self, method, url, **request_kwargs):
                    raise failure
                    yield  # pragma: no cover

            yield Client()

    row = dict(CUSTOM_ROW)
    http = FailingHttp()
    binding = await create_plugin().bind(
        spec("custom_http", row), OWNER,
        PluginHost(configuration={}, extensions=Extensions(row), public_http=http),
    )
    result = await binding.execute({}, OWNER)
    assert result.status == "failed" and result.error_code == "custom_http_call_failed"
    assert expected in result.model_content and not result.display


@pytest.mark.asyncio
@pytest.mark.parametrize("compressed", [False, True])
async def test_custom_http_rejects_large_or_compressed_stream_without_display(compressed):
    class UnsafeResponse(StreamResponse):
        headers = {"content-encoding": "gzip"} if compressed else {"content-encoding": "identity"}

        async def aiter_bytes(self):
            if compressed:
                raise AssertionError("compressed content must not be iterated")
            yield b"x" * (64 * 1024 + 1)

    class UnsafeHttp(Http):
        @asynccontextmanager
        async def client(self, **kwargs):
            class Client:
                @asynccontextmanager
                async def stream(self, method, url, **request_kwargs):
                    yield UnsafeResponse()

            yield Client()

    row = dict(CUSTOM_ROW)
    binding = await create_plugin().bind(
        spec("custom_http", row), OWNER,
        PluginHost(configuration={}, extensions=Extensions(row), public_http=UnsafeHttp()),
    )
    result = await binding.execute({}, OWNER)
    assert result.status == "failed" and not result.display
    assert result.error_code == ("custom_http_compressed_response" if compressed else "custom_http_response_too_large")


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [
    {"url": "https://changed.example/x"}, {"config_version": 2}, {"description": "Changed"},
    {"timeout_seconds": 60}, {"params_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "additionalProperties": False}},
    {"effect": "external_mutation"}, {"method": "POST"}, {"load_policy": "on_demand"}, {"is_active": False},
])
async def test_custom_http_revocation_prevents_outbound_request(changed):
    row = dict(CUSTOM_ROW)
    store, http = Extensions(row), Http()
    binding = await create_plugin().bind(spec("custom_http", row), OWNER, PluginHost(configuration={}, extensions=store, public_http=http))
    store.row = {**row, **changed}
    with pytest.raises(PluginError) as exc:
        await binding.execute({}, OWNER)
    assert exc.value.code == "plugin_configuration_changed"
    assert http.stream_requests == []


@pytest.mark.asyncio
async def test_custom_http_rechecks_in_place_schema_changes():
    row = {**CUSTOM_ROW, "params_schema": {"type": "object", "properties": {}, "additionalProperties": False}}
    store, http = Extensions(row), Http()
    binding = await create_plugin().bind(spec("custom_http", row), OWNER, PluginHost(configuration={}, extensions=store, public_http=http))
    row["params_schema"]["properties"]["secret"] = {"type": "string"}
    with pytest.raises(PluginError) as exc:
        await binding.execute({}, OWNER)
    assert exc.value.code == "plugin_configuration_changed"
    assert http.stream_requests == []

@pytest.mark.asyncio
async def test_custom_http_rechecks_identity_before_network():
    row = dict(CUSTOM_ROW)
    store, http = Extensions(row), Http()
    binding = await create_plugin().bind(spec("custom_http", row), OWNER, PluginHost(configuration={}, extensions=store, public_http=http))
    changed = ExecutionContext(user_id="owner", project_id="p", run_id="r", identity=IDENTITY.model_copy(update={"config_fingerprint": "1" * 64}))
    with pytest.raises(PluginError) as exc:
        await binding.execute({}, changed)
    assert exc.value.code == "plugin_authority_revoked"
    assert len(store.namespaces) == 1 and http.stream_requests == []


@pytest.mark.asyncio
async def test_managed_fetch_blocks_domain_before_network_and_extracts_html():
    http = Http()
    host = PluginHost(configuration={}, public_http=http)
    binding = await create_plugin().bind(spec("managed_web_fetch", {"max_uses": 1, "allowed_domains": ["docs.example"]}), OWNER, host)
    denied = await binding.execute({"url": "https://outside.example/private"}, OWNER)
    assert denied.status == "failed" and http.requests == []
    allowed = await binding.execute({"url": "https://docs.example/start"}, OWNER)
    assert allowed.status == "completed" and "Helpful text" in allowed.model_content
    assert "skip" not in allowed.model_content
    assert http.requests == [("GET", "https://docs.example/start")]
    assert allowed.display == [ToolTextPart(text=allowed.model_content)]
    assert [item["kind"] for item in allowed.usage_components["components"]] == ["web_fetch_requests", "web_fetch_context"]


@pytest.mark.asyncio
async def test_managed_fetch_checks_every_redirect_and_limit():
    http = Http()
    http.responses["https://docs.example/start"] = (302, {"location": "https://outside.example/private"}, b"")
    host = PluginHost(configuration={}, public_http=http)
    plugin = create_plugin()
    binding = await plugin.bind(spec("managed_web_fetch", {"max_uses": 1, "allowed_domains": ["docs.example"]}), OWNER, host)
    denied = await binding.execute({"url": "https://docs.example/start"}, OWNER)
    assert denied.status == "failed" and denied.error_code == "managed_fetch_failed"
    assert http.requests == [("GET", "https://docs.example/start")]
    limited = await plugin.bind(spec("managed_web_fetch", {"max_uses": 1, "current_use_count": 1}), OWNER, host)
    result = await limited.execute({"url": "https://docs.example/start"}, OWNER)
    assert result.error_code == "managed_tool_limit_reached" and len(http.requests) == 1


@pytest.mark.asyncio
async def test_managed_search_and_advisor_keep_route_context_and_budget_hooks():
    advisor = Advisor()
    host = PluginHost(configuration={}, advisor=advisor)
    plugin = create_plugin()
    search_config = {"max_uses": 1, "route": {"model": "search"}, "context_size": "short", "allowed_domains": ["docs.example"], "blocked_domains": ["bad.example"], "country": "KR"}
    search = await plugin.bind(spec("managed_web_search", search_config), OWNER, host)
    result = await search.execute({"query": " docs "}, OWNER)
    assert result.status == "completed" and result.display == [ToolTextPart(text="provider answer")]
    assert result.usage_components == {"components": [{"kind": "provider"}]}
    assert advisor.calls == [({"kind": "search", "query": "docs", "route": {"model": "search"}, "context_size": "short", "allowed_domains": ["docs.example"], "blocked_domains": ["bad.example"], "country": "KR"}, OWNER)]
    advisor.calls.clear()
    advisor_binding = await plugin.bind(spec("managed_advisor", {"max_uses": 1, "route": {"model": "reasoner"}, "visible_messages": [{"role": "user", "content": "context"}]}), OWNER, host)
    answer = await advisor_binding.execute({"goal": " explain "}, OWNER)
    assert answer.model_content == "provider answer"
    assert advisor.calls == [({"kind": "advisor", "goal": "explain", "route": {"model": "reasoner"}, "visible_messages": [{"role": "user", "content": "context"}]}, OWNER)]
    advisor.calls.clear()
    limited = await plugin.bind(spec("managed_web_search", {**search_config, "current_use_count": 1}), OWNER, host)
    assert (await limited.execute({"query": "docs"}, OWNER)).error_code == "managed_tool_limit_reached"
    assert (await advisor_binding.execute({"goal": ""}, OWNER)).error_code == "invalid_advisor_goal"
    assert advisor.calls == []
