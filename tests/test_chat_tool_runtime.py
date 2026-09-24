"""툴 런타임(내장 + 동적 커스텀 HTTP 툴) 실행 테스트.

- context_execute 디스패치: 내장 우선, 커스텀 툴, 미등록.
- 커스텀 HTTP 실행: SSRF 차단 시 안전 문자열, 성공 시 응답 요약.
- 동적 스키마: 내장 + 커스텀 병합. 저장소 장애 시 graceful(내장만).
"""

import json

import pytest
from lumen_plugin_api.contracts import ExecutionContext, PluginError
from lumen_plugin_api.tools import ToolBinding, ToolDefinition
from lumen_plugin_api.tools import ToolExecutionResult as V2ToolExecutionResult

from lumen.services import conversation_store as cs
from lumen.services.tool_runtime import bindings, contracts, selection
from lumen.services.tool_runtime import dispatch as tool_runtime
from lumen.services.tools import ToolContext

_CTX = ToolContext(project_id="p1", user_id="u1")

def _custom_row(**overrides):
    return {
        "id": 1, "name": "weather", "description": "날씨", "method": "GET",
        "url": "https://api.example/w", "timeout_seconds": 5,
        "params_schema": {"type": "object", "properties": {}}, "effect": "read",
        "config_version": 1, "load_policy": "preloaded", "is_active": True,
        **overrides,
    }


def _custom_host(monkeypatch, rows):
    from lumen.plugins.tools_host import ToolsExtensionAccess

    rows = rows if isinstance(rows, list) else [rows]

    async def resolve(self, kind, identifier, namespace):
        assert kind == "tool" and namespace.user_id == "u1" and namespace.project_id == "p1"
        return next(row for row in rows if row["id"] == identifier)

    monkeypatch.setattr(ToolsExtensionAccess, "resolve", resolve)


class _Resp:
    def __init__(self, status=200, text="hello world"):
        self.status_code = status
        self._text = text
        self.encoding = "utf-8"
        self.headers = {}

    async def aiter_bytes(self):
        yield self._text.encode()


class _Stream:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *args):
        return False


class _Client:
    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def stream(self, method, url, **kwargs):
        return _Stream(_Resp(text="posted" if method == "POST" else "hello world"))


class _ManagedHooks:
    def __init__(self, allowed: bool = True):
        self.allowed = allowed
        self.calls: list[tuple[str, int]] = []

    async def managed_tool_allowed(self, *, tool_name: str, maximum: int) -> bool:
        self.calls.append((tool_name, maximum))
        return self.allowed


class TestSchemas:
    async def test_builtin_plus_custom(self, monkeypatch):
        row = _custom_row()
        _custom_host(monkeypatch, row)

        async def fake_load(ctx):
            return [row]

        monkeypatch.setattr(selection, "_load_custom", fake_load)
        monkeypatch.setattr(bindings, "_load_custom", fake_load)

        async def no_mcp(_ctx):
            return []

        monkeypatch.setattr(bindings, "_load_mcp", no_mcp)
        schemas = await selection.context_tool_schemas(_CTX)
        names = {s["function"]["name"] for s in schemas}
        assert "list_my_conversations" in names
        assert any(name.startswith("custom__1__weather_") for name in names)
        runtime = await bindings.v2_tool_bindings(_CTX, include_platform=False)
        custom = next(binding for binding in runtime.values() if binding.definition.source == "custom_http")
        projected = next(schema["function"] for schema in schemas if schema["function"]["name"] == custom.definition.name)
        assert projected == {
            "name": custom.definition.name,
            "description": custom.definition.description,
            "parameters": custom.definition.input_schema,
        }
        assert await selection.context_tool_activity_metadata(custom.definition.name, _CTX) == (
            "custom_http", "커스텀 도구"
        )

    async def test_graceful_when_storage_fails(self, monkeypatch):
        # _load_custom 내부 예외 → 내장 툴만 (graph 가 죽지 않게)
        schemas = await selection.context_tool_schemas(_CTX)  # 실 DB 없음 → graceful
        names = {s["function"]["name"] for s in schemas}
        assert "list_my_conversations" in names


class TestManagedTools:
    async def test_managed_tools_are_gated_by_durable_hook_and_emit_schemas(self, monkeypatch):
        async def no_custom(ctx):
            return []

        monkeypatch.setattr(selection, "_load_custom", no_custom)
        hooks = _ManagedHooks()
        ctx = ToolContext(
            project_id="p1",
            user_id="u1",
            execution_hooks=hooks,
            managed_search={
                "route": {"provider_type": "perplexity"},
                "options": {"max_uses": 2, "context_size": "low"},
            },
            managed_fetch={"max_uses": 1},
        )
        names = {schema["function"]["name"] for schema in await selection.context_tool_schemas(ctx)}
        assert {"managed_web_search", "managed_web_fetch"} <= names

        from lumen.plugins import tools_host

        async def fake_search(*args, **kwargs):
            return [tools_host.web_search.SearchCitation(url="https://docs.example/a", title="A", snippet="B")]

        monkeypatch.setattr(tools_host.web_search, "search_with_route", fake_search)
        result = await tool_runtime.context_execute("managed_web_search", {"query": "docs"}, ctx)
        assert json.loads(result)["sources"][0]["url"] == "https://docs.example/a"
        assert hooks.calls == [("managed_web_search", 2)]

    async def test_managed_tool_limit_stops_before_provider_io(self, monkeypatch):
        hooks = _ManagedHooks(allowed=False)
        ctx = ToolContext(
            project_id="p1",
            user_id="u1",
            execution_hooks=hooks,
            managed_search={
                "route": {"provider_type": "perplexity"},
                "options": {"max_uses": 1, "context_size": "low"},
            },
        )

        async def unexpected(*args, **kwargs):
            raise AssertionError("provider must not be called after durable limit")

        from lumen.plugins import tools_host

        monkeypatch.setattr(tools_host.web_search, "search_with_route", unexpected)
        assert "한도" in await tool_runtime.context_execute("managed_web_search", {"query": "docs"}, ctx)

    async def test_managed_advisor_result_stays_private_from_graph_projection(self, monkeypatch):
        async def no_custom(ctx):
            return []

        monkeypatch.setattr(selection, "_load_custom", no_custom)
        hooks = _ManagedHooks()
        ctx = ToolContext(
            project_id="p1",
            user_id="u1",
            execution_hooks=hooks,
            managed_advisor={
                "route": {"model_name": "advisor-model"},
                "options": {"max_uses": 2},
            },
            advisor_visible_messages=({"role": "user", "content": "visible context"},),
        )

        class _Result:
            advice = "private advice"
            prompt_tokens = 3
            completion_tokens = 2

        async def fake_advisor(**kwargs):
            assert kwargs["visible_messages"] == [{"role": "user", "content": "visible context"}]
            return _Result()

        from lumen.plugins import tools_host

        monkeypatch.setattr(tools_host.advisor_service, "ask_with_route", fake_advisor)
        names = {schema["function"]["name"] for schema in await selection.context_tool_schemas(ctx)}
        assert "managed_advisor" in names
        result = await tool_runtime.context_execute_result("managed_advisor", {"goal": "review"}, ctx)
        assert result.content == "private advice"
        assert result.visible is False
        assert hooks.calls == [("managed_advisor", 2)]


class TestDispatch:
    async def test_builtin_dispatch(self, monkeypatch):
        async def fake_list(**kwargs):
            return [{"id": "c1", "title": "t", "model_name": "m"}]

        monkeypatch.setattr(cs, "list_conversations", fake_list)
        out = await tool_runtime.context_execute("list_my_conversations", {}, _CTX)
        assert "t" in out

    async def test_unknown_tool(self, monkeypatch):
        async def fake_load(ctx):
            return []

        monkeypatch.setattr(selection, "_load_custom", fake_load)
        out = await tool_runtime.context_execute("no_such", {}, _CTX)
        assert "알 수 없는" in out

    async def test_custom_tool_dispatch_success(self, monkeypatch):
        row = _custom_row()
        _custom_host(monkeypatch, row)

        async def fake_load(ctx):
            return [row]

        monkeypatch.setattr(selection, "_load_custom", fake_load)
        monkeypatch.setattr("httpx.AsyncClient", _Client)
        schema = next(item for item in await selection.context_tool_schemas(_CTX) if item["function"]["name"].startswith("custom__"))
        out = await tool_runtime.context_execute(schema["function"]["name"], {}, _CTX)
        assert out.startswith("[200]")
        assert "hello world" in out



class TestRegistryToolBindings:
    async def test_unknown_default_export_fails_closed(self):
        from lumen.plugins.tools_host import bind_default_tool

        with pytest.raises(PluginError) as exc:
            await bind_default_tool("not_an_installed_export", ExecutionContext(user_id="u1", project_id="p1"))
        assert exc.value.code == "plugin_unavailable"

    async def test_selected_plugin_snapshot_dispatches_with_plugin_source(self, monkeypatch):
        from lumen.plugins import bindings as plugin_bindings
        from lumen.services.agent_runtime_v2 import dispatch_tool_call

        identifier = "123e4567-e89b-12d3-a456-426614174000"
        row = {
            "id": identifier, "kind": "tool", "plugin_id": "default-tools",
            "export_key": "list_my_conversations", "name": "Selected conversations",
            "scope": "user", "owner_user_id": "u1", "owner_project_id": "p1",
            "config": {}, "config_version": 1, "is_active": True,
        }

        async def resolve_binding(value, *, kind, namespace):
            assert value == identifier and kind == "tool"
            assert namespace.user_id == "u1" and namespace.project_id == "p1"
            return row

        async def list_conversations(**kwargs):
            assert kwargs["user_id"] == "u1" and kwargs["project_id"] == "p1"
            return [{"id": "c1", "title": "Mine", "model_name": "m"}]

        async def no_extensions(_ctx):
            return []

        monkeypatch.setattr(plugin_bindings, "resolve_binding", resolve_binding)
        monkeypatch.setattr(cs, "list_conversations", list_conversations)
        monkeypatch.setattr(bindings, "_load_custom", no_extensions)
        monkeypatch.setattr(bindings, "_load_mcp", no_extensions)
        snapshot, = await plugin_bindings.freeze_bindings(
            [identifier], kind="tool", user_id="u1", project_id="p1"
        )
        ctx = ToolContext(project_id="p1", user_id="u1", plugin_tool_snapshots=(snapshot,))
        bound = await bindings.v2_tool_bindings(ctx)
        plugin_binding = next(binding for binding in bound.values() if binding.definition.source == "plugin")

        result = await dispatch_tool_call(plugin_binding, {}, ctx)

        assert plugin_binding.definition.name.startswith("plugin__")
        assert result.status == "completed"
        assert "Mine" in result.model_content


class TestSelectionFilter:
    def test_none_all_empty_subset(self):
        items = [{"id": 1}, {"id": 2}, {"id": 3}]
        assert selection._selected(items, None) == items  # None=전체
        assert selection._selected(items, ()) == []  # 빈=없음
        assert selection._selected(items, (1, 3)) == [{"id": 1}, {"id": 3}]  # 부분


class TestMcpTools:
    async def test_selected_mcp_tool_prefixed_schema(self, monkeypatch):
        import lumen.services.extensions_store as es

        async def fake_list_for_user(kind, *, user_id, project_id, active_only=False, reveal_secrets=False):
            if kind == "mcp":
                return [{"id": 7, "name": "srv", "transport": "http", "url": "https://mcp.example/x"}]
            return []  # 커스텀 tool 없음

        async def fake_list_tools(server):
            return [{"name": "search", "description": "d", "input_schema": {"type": "object"}}]

        monkeypatch.setattr(es, "list_for_user", fake_list_for_user)
        monkeypatch.setattr(selection.mcp_client, "list_tools", fake_list_tools)
        schemas = await selection.context_tool_schemas(_CTX)
        names = {s["function"]["name"] for s in schemas}
        assert "mcp__7__search" in names  # server_id 접두

    async def test_mcp_name_routes_to_call_tool(self, monkeypatch):
        import lumen.services.extensions_store as es

        captured = {}

        async def fake_list_for_user(kind, *, user_id, project_id, active_only=False, reveal_secrets=False):
            if kind == "mcp":
                return [{"id": 7, "name": "srv", "transport": "http", "url": "https://mcp.example/x"}]
            return []

        async def fake_call_tool(server, tool_name, args):
            captured.update(server_id=server["id"], tool=tool_name, args=args)
            return "결과"

        monkeypatch.setattr(es, "list_for_user", fake_list_for_user)
        monkeypatch.setattr(selection.mcp_client, "call_tool", fake_call_tool)
        out = await tool_runtime.context_execute("mcp__7__search", {"q": "x"}, _CTX)
        assert out == "결과"
        assert captured == {"server_id": 7, "tool": "search", "args": {"q": "x"}}

    async def test_mcp_unknown_server_safe_string(self, monkeypatch):
        import lumen.services.extensions_store as es

        async def fake_list_for_user(kind, *, user_id, project_id, active_only=False, reveal_secrets=False):
            return []

        monkeypatch.setattr(es, "list_for_user", fake_list_for_user)
        out = await tool_runtime.context_execute("mcp__99__x", {}, _CTX)
        assert "MCP" in out  # 안전한 거부

    async def test_legacy_mcp_transport_is_not_exposed(self, monkeypatch):
        import lumen.services.extensions_store as es

        async def fake_list_for_user(kind, *, user_id, project_id, active_only=False, reveal_secrets=False):
            return [{"id": 7, "name": "legacy", "transport": "sse", "url": "https://mcp.example/sse"}]

        monkeypatch.setattr(es, "list_for_user", fake_list_for_user)
        assert await selection._load_mcp(_CTX) == []

    async def test_unavailable_mcp_secret_blocks_discovery_without_network_io(self, monkeypatch):
        import lumen.services.extensions_store as es

        async def secret_unavailable(*_args, **_kwargs):
            raise es.ExtensionSecretUnavailable("cannot decrypt")

        async def unexpected_network_io(*_args, **_kwargs):
            raise AssertionError("MCP network I/O must not occur")

        monkeypatch.setattr(es, "list_for_user", secret_unavailable)
        monkeypatch.setattr(selection.mcp_client, "list_tools", unexpected_network_io)

        schemas = await selection.context_tool_schemas(_CTX)

        assert all(not schema["function"]["name"].startswith("mcp__") for schema in schemas)

    async def test_rotated_credential_blocks_mcp_dispatch_without_network_io(self, monkeypatch):
        import lumen.services.extensions_store as es

        async def fake_list_for_user(*_args, **_kwargs):
            return [{"id": 7, "transport": "http", "url": "https://mcp.example", "headers": {}}]

        async def rotated_version(*_args, **_kwargs):
            return {7: 2}

        async def unexpected_network_io(*_args, **_kwargs):
            raise AssertionError("MCP network I/O must not occur")

        monkeypatch.setattr(es, "list_for_user", fake_list_for_user)
        monkeypatch.setattr(es, "mcp_credential_versions", rotated_version)
        monkeypatch.setattr(selection.mcp_client, "call_tool", unexpected_network_io)

        output = await tool_runtime.context_execute(
            "mcp__7__search",
            {"q": "x"},
            ToolContext(
                project_id="p1",
                user_id="u1",
                selected_mcp_ids=(7,),
                expected_mcp_credential_versions=((7, 1),),
            ),
        )

        assert output == "선택되지 않았거나 접근 불가한 MCP 서버입니다."

    async def test_admin_approved_static_headers_reach_only_that_server(self, monkeypatch):
        import lumen.services.extensions_store as es

        async def fake_list_for_user(kind, *, user_id, project_id, active_only=False, reveal_secrets=False):
            if kind == "mcp":
                return [
                    {
                        "id": 7,
                        "name": "admin-approved",
                        "transport": "http",
                        "url": "https://mcp.example",
                        "headers": {"X-Api-Key": "administrator-secret"},
                        "auth_mode": "admin",
                    }
                ]
            return []

        monkeypatch.setattr(es, "list_for_user", fake_list_for_user)

        servers = await selection._load_mcp(_CTX)

        assert servers == [
            {
                "id": 7,
                "name": "admin-approved",
                "transport": "http",
                "url": "https://mcp.example",
                "headers": {"X-Api-Key": "administrator-secret"},
                "auth_mode": "admin",
            }
        ]

    async def test_disconnected_oauth_server_is_not_exposed(self, monkeypatch):
        import lumen.services.extensions_store as es
        import lumen.services.mcp_oauth as oauth

        async def fake_list_for_user(*_args, **_kwargs):
            return [
                {
                    "id": 7,
                    "name": "notion",
                    "transport": "http",
                    "url": "https://mcp.notion.com/mcp",
                    "headers": {},
                    "auth_mode": "oauth",
                }
            ]

        async def no_headers(*, user_id, project_id):
            return {}

        async def connected_headers(*, user_id, project_id):
            return {7: {"Authorization": "Bearer user-oauth-token"}}

        monkeypatch.setattr(es, "list_for_user", fake_list_for_user)
        monkeypatch.setattr(oauth, "headers_for_user", no_headers)
        assert await selection._load_mcp(_CTX) == []

        monkeypatch.setattr(oauth, "headers_for_user", connected_headers)
        servers = await selection._load_mcp(_CTX)
        assert servers[0]["headers"]["Authorization"] == "Bearer user-oauth-token"


class TestDeferredToolBinding:
    async def test_catalog_binds_at_most_requested_on_demand_schemas(self, monkeypatch):
        tools = [
            {
                "id": identifier,
                "name": f"weather_{identifier}",
                "description": "Retrieve weather forecasts.",
                "method": "GET",
                "url": f"https://api{identifier}.example/weather",
                "params_schema": {"type": "object", "properties": {}},
                "effect": "read",
                "config_version": 1,
                "load_policy": "on_demand",
                "is_active": True,
            }
            for identifier in range(1, 11)
        ]

        async def fake_custom(_ctx):
            return tools

        async def fake_mcp(_ctx):
            return []

        _custom_host(monkeypatch, tools)
        monkeypatch.setattr(bindings, "_load_custom", fake_custom)
        monkeypatch.setattr(bindings, "_load_mcp", fake_mcp)
        ctx = ToolContext(
            project_id="p1",
            user_id="u1",
            binding_session=contracts.ToolBindingSession(),
        )

        initial = await selection.context_tool_schemas(ctx)
        initial_names = {schema["function"]["name"] for schema in initial}
        assert "list_available_tools" in initial_names
        assert not any(name.startswith("custom__") for name in initial_names)

        result = await tool_runtime.context_execute_result(
            "list_available_tools",
            {"query": "weather", "max_results": 3, "category": "커스텀"},
            ctx,
        )
        loaded = json.loads(result.content)
        assert len(loaded["tools"]) == 3
        assert all(len(item["description"]) <= 280 for item in loaded["tools"])

        loaded_names = {schema["function"]["name"] for schema in await selection.context_tool_schemas(ctx)}
        assert set(loaded["loaded_tools"]) <= loaded_names

    async def test_catalog_loads_safe_mcp_schema_for_natural_language_mcp_query(self, monkeypatch):
        async def no_custom(_ctx):
            return []

        async def mcp_server(_ctx):
            return [
                {
                    "id": 7,
                    "name": "Notion",
                    "url": "https://mcp.example.test",
                    "effect_overrides": {},
                    "config_version": 1,
                    "load_policy": "on_demand",
                }
            ]

        async def discovered_tools(_server):
            return [
                {
                    "name": "notion-search",
                    "description": "Search the connected Notion workspace.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "filter": {
                                "type": "object",
                                "properties": {"query": {"type": "string"}},
                                "additionalProperties": {},
                            }
                        },
                        "additionalProperties": {},
                    },
                },
                {
                    "name": "notion-update-properties",
                    "description": "Update a page with dynamic property names.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "properties": {"type": "object", "additionalProperties": {}},
                        },
                    },
                },
            ]

        monkeypatch.setattr(bindings, "_load_custom", no_custom)
        monkeypatch.setattr(bindings, "_load_mcp", mcp_server)
        monkeypatch.setattr(bindings.mcp_client, "list_tools", discovered_tools)
        ctx = ToolContext(
            project_id="p1",
            user_id="u1",
            binding_session=contracts.ToolBindingSession(),
        )

        result = await tool_runtime.context_execute_result(
            "list_available_tools",
            {"query": "현재 연결되어 사용 가능한 MCP 서버와 MCP 도구 목록"},
            ctx,
        )
        loaded = json.loads(result.content)

        assert len(loaded["loaded_tools"]) == 1
        assert loaded["loaded_tools"][0].startswith("mcp__7__notion_search")

    async def test_bound_extension_is_revalidated_before_dispatch(self, monkeypatch):
        import lumen.services.extensions_store as es

        original = {
            "id": 1,
            "name": "weather",
            "description": "Retrieve weather forecasts.",
            "method": "GET",
            "url": "https://api.example/weather",
            "params_schema": {"type": "object", "properties": {}},
            "effect": "read",
            "config_version": 1,
            "load_policy": "on_demand",
            "is_active": True,
        }
        current = dict(original)
        _custom_host(monkeypatch, current)

        async def fake_custom(context):
            return selection._frozen_selection([current], context, "tool")

        async def fake_mcp(_ctx):
            return []

        def unexpected_network(*_args, **_kwargs):
            raise AssertionError("changed extension must not dispatch")

        monkeypatch.setattr(bindings, "_load_custom", fake_custom)
        monkeypatch.setattr(bindings, "_load_mcp", fake_mcp)
        from lumen.plugins.tools_host import ToolsPublicHttpAccess
        monkeypatch.setattr(ToolsPublicHttpAccess, "client", unexpected_network)
        ctx = ToolContext(
            project_id="p1",
            user_id="u1",
            selected_tool_ids=(1,),
            expected_extension_fingerprints=(("tool", 1, es.selection_fingerprint(original)),),
            binding_session=contracts.ToolBindingSession(),
        )
        bound_bindings = await bindings.v2_tool_bindings(
            ctx,
            extension_load_policy="on_demand",
            include_platform=False,
        )
        binding = next(iter(bound_bindings.values()))
        current["config_version"] = 2

        result = await binding.execute({}, ctx)

        assert result.error_code == "extension_unavailable"

    async def test_v1_catalog_cannot_load_or_dispatch_managed_mutation(self, monkeypatch):
        mutation_name = "managed_delete_cloud_instance"
        executed = False
        include_managed_requests: list[bool] = []

        async def execute_mutation(_args, _context):
            nonlocal executed
            executed = True
            return V2ToolExecutionResult(status="completed", model_content="mutation dispatched")

        mutation_binding = ToolBinding(
            definition=ToolDefinition(
                name=mutation_name,
                description="Delete a cloud instance.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                effect="external_mutation",
                source="managed",
                activity_category="클라우드 제어",
            ),
            execute=execute_mutation,
        )

        async def fake_v2_bindings(
            _ctx,
            *,
            extension_load_policy=None,
            include_platform=True,
            include_managed=False,
        ):
            include_managed_requests.append(include_managed)
            if extension_load_policy == "on_demand" and include_managed:
                return {mutation_name: mutation_binding}
            return {}

        monkeypatch.setattr(bindings, "v2_tool_bindings", fake_v2_bindings)
        ctx = ToolContext(
            project_id="p1",
            user_id="u1",
            binding_session=contracts.ToolBindingSession(),
        )

        catalog_result = await tool_runtime.context_execute_result(
            "list_available_tools",
            {"query": "delete cloud"},
            ctx,
        )
        loaded = json.loads(catalog_result.content)
        schema_names = {schema["function"]["name"] for schema in await selection.context_tool_schemas(ctx)}
        dispatch_result = await tool_runtime.context_execute_result(mutation_name, {}, ctx)

        assert loaded["loaded_tools"] == []
        assert mutation_name not in schema_names
        assert dispatch_result.content == f"알 수 없는 툴입니다: {mutation_name}"
        assert executed is False
        assert include_managed_requests and not any(include_managed_requests)

    async def test_v1_deferred_replay_rejects_managed_mutation(self, monkeypatch):
        mutation_name = "managed_delete_cloud_instance"

        async def fake_v2_bindings(_ctx, *, include_managed=False, **_kwargs):
            return {mutation_name: object()} if include_managed else {}

        monkeypatch.setattr(bindings, "v2_tool_bindings", fake_v2_bindings)
        ctx = ToolContext(
            project_id="p1",
            user_id="u1",
            binding_session=contracts.ToolBindingSession(),
        )

        try:
            await bindings.restore_v2_deferred_bindings(
                ctx,
                ctx.binding_session.legacy_bindings,
                [mutation_name],
                include_managed=False,
            )
        except RuntimeError as exc:
            assert str(exc) == "a previously loaded tool is no longer eligible for this run"
        else:
            raise AssertionError("v1 replay must not restore managed mutation bindings")

        assert mutation_name not in ctx.binding_session.legacy_bindings
