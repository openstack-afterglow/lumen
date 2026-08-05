import pytest
from pydantic import ValidationError

from lumen.services import tool_runtime
from lumen.services.agent_protocol import (
    AgentExecutionPolicy,
    ToolBinding,
    ToolDefinition,
    ToolExecutionResult,
    validate_tool_arguments,
)
from lumen.services.agent_runtime_v2 import dispatch_tool_call
from lumen.services.tool_runtime import v2_builtin_tool_bindings
from lumen.services.tools import ToolContext


def test_tool_definition_rejects_provider_invalid_names_and_oversized_effect_surface():
    with pytest.raises(ValidationError):
        ToolDefinition(
            name="custom__1__invalid-name",
            description="invalid provider name",
            input_schema={"type": "object"},
            effect="read",
            source="custom_http",
        )

    definition = ToolDefinition(
        name="custom_1_status",
        description="Read a bounded status value.",
        input_schema={
            "type": "object",
            "properties": {"resource": {"type": "string", "maxLength": 64}},
            "required": ["resource"],
            "additionalProperties": False,
        },
        effect="read",
        source="custom_http",
    )
    assert definition.parallel_safe is False

    with pytest.raises(ValidationError, match="additionalProperties"):
        ToolDefinition(
            name="custom_2_open_object",
            description="Unsafe schema.",
            input_schema={"type": "object", "properties": {"value": {"type": "string"}}},
            effect="read",
            source="custom_http",
        )


def test_tool_arguments_are_validated_before_dispatch():
    schema = {
        "type": "object",
        "properties": {"resource": {"type": "string", "enum": ["status"]}},
        "required": ["resource"],
        "additionalProperties": False,
    }

    assert validate_tool_arguments(schema, {"resource": "status"}) == {"resource": "status"}
    with pytest.raises(ValueError, match="frozen schema"):
        validate_tool_arguments(schema, {"resource": "other"})
    with pytest.raises(ValueError, match="frozen schema"):
        validate_tool_arguments(schema, {"resource": "status", "extra": True})


def test_tool_result_is_bounded_to_safe_display_parts_and_agent_policy_caps():
    result = ToolExecutionResult(
        status="completed",
        model_content="ok",
        display=[{"type": "code", "code": "print('ok')", "language": "python"}],
    )
    assert result.display[0].type == "code"

    with pytest.raises(ValidationError):
        AgentExecutionPolicy(
            allowed_modes={"code"},
            max_model_turns=17,
            max_tool_calls=24,
            max_children=0,
            max_parallel_children=0,
            max_child_depth=0,
        )


async def test_v2_dispatch_validates_before_executing_binding():
    called = False

    async def execute(arguments, context):
        nonlocal called
        called = True
        return ToolExecutionResult(status="completed", model_content=arguments["resource"])

    binding = ToolBinding(
        definition=ToolDefinition(
            name="custom_1_status",
            description="Read status.",
            input_schema={
                "type": "object",
                "properties": {"resource": {"type": "string", "enum": ["status"]}},
                "required": ["resource"],
                "additionalProperties": False,
            },
            effect="read",
            source="custom_http",
        ),
        execute=execute,
    )

    invalid = await dispatch_tool_call(binding, {"resource": "other"}, object())

    assert invalid.status == "failed"
    assert invalid.error_code == "invalid_tool_arguments"
    assert called is False


def test_v2_builtin_bindings_are_read_only_and_schema_closed():
    bindings = v2_builtin_tool_bindings()

    assert set(bindings) == {"list_my_conversations", "get_conversation_detail"}
    detail = bindings["get_conversation_detail"].definition
    assert detail.effect == "read"
    assert detail.parallel_safe is True
    assert detail.source == "builtin"
    assert detail.input_schema["additionalProperties"] is False


async def test_v2_dynamic_bindings_namespace_names_and_freeze_effects(monkeypatch):
    async def load_custom(_ctx):
        return [
            {
                "id": 7,
                "name": "status-check",
                "description": "Read one status.",
                "params_schema": {
                    "type": "object",
                    "properties": {"resource": {"type": "string", "enum": ["status"]}},
                    "required": ["resource"],
                },
                "effect": "read",
                "url": "https://API.Example.com/status",
            }
        ]

    async def load_mcp(_ctx):
        return [{"id": 8, "url": "https://Mcp.Example.com/api", "effect_overrides": {"inspect-status": "read"}}]

    async def list_tools(_server):
        return [
            {
                "name": "inspect-status",
                "description": "Inspect one status.",
                "input_schema": {
                    "type": "object",
                    "properties": {"resource": {"type": "string"}},
                    "required": ["resource"],
                },
            }
        ]

    monkeypatch.setattr(tool_runtime, "_load_custom", load_custom)
    monkeypatch.setattr(tool_runtime, "_load_mcp", load_mcp)
    monkeypatch.setattr(tool_runtime.mcp_client, "list_tools", list_tools)

    bindings = await tool_runtime.v2_tool_bindings(ToolContext(project_id="project", user_id="user"))
    dynamic = [binding.definition for binding in bindings.values() if binding.definition.source != "builtin"]

    assert len(dynamic) == 2
    assert {definition.source for definition in dynamic} == {"custom_http", "mcp"}
    assert {definition.effect for definition in dynamic} == {"read"}
    assert all(definition.name.startswith(("custom__7__", "mcp__8__")) for definition in dynamic)
    assert all(definition.input_schema["additionalProperties"] is False for definition in dynamic)
    by_source = {binding.definition.source: binding for binding in bindings.values()}
    assert by_source["custom_http"].destination_origin == "https://api.example.com"
    assert by_source["mcp"].destination_origin == "https://mcp.example.com"


async def test_v2_dynamic_binding_dispatches_only_schema_validated_arguments(monkeypatch):
    seen: list[dict[str, object]] = []

    async def load_custom(_ctx):
        return [
            {
                "id": 9,
                "name": "status",
                "params_schema": {
                    "type": "object",
                    "properties": {"resource": {"type": "string", "enum": ["status"]}},
                    "required": ["resource"],
                },
                "url": "https://api.example.com/status",
            }
        ]

    async def load_mcp(_ctx):
        return []

    async def execute_custom(_tool, arguments, _ctx):
        seen.append(arguments)
        return "healthy"

    monkeypatch.setattr(tool_runtime, "_load_custom", load_custom)
    monkeypatch.setattr(tool_runtime, "_load_mcp", load_mcp)
    monkeypatch.setattr(tool_runtime, "_execute_custom_http_tool", execute_custom)

    bindings = await tool_runtime.v2_tool_bindings(ToolContext(project_id="project", user_id="user"))
    binding = next(binding for binding in bindings.values() if binding.definition.source == "custom_http")
    context = ToolContext(project_id="project", user_id="user")

    valid = await dispatch_tool_call(binding, {"resource": "status"}, context)
    invalid = await dispatch_tool_call(binding, {"resource": "status", "extra": True}, context)

    assert valid.status == "completed"
    assert valid.model_content == "healthy"
    assert invalid.error_code == "invalid_tool_arguments"
    assert seen == [{"resource": "status"}]


async def test_v2_remote_bindings_require_canonical_destinations(monkeypatch):
    async def load_custom(_ctx):
        return [{"id": 9, "name": "missing-url"}]

    async def load_mcp(_ctx):
        return [{"id": 10, "url": "https://localhost/api"}]

    async def list_tools(_server):
        raise AssertionError("invalid MCP destinations must not be discovered")

    monkeypatch.setattr(tool_runtime, "_load_custom", load_custom)
    monkeypatch.setattr(tool_runtime, "_load_mcp", load_mcp)
    monkeypatch.setattr(tool_runtime.mcp_client, "list_tools", list_tools)

    bindings = await tool_runtime.v2_tool_bindings(ToolContext(project_id="project", user_id="user"))

    assert set(bindings) == set(v2_builtin_tool_bindings())


async def test_lumen_bindings_freeze_only_the_selected_grant_snapshot(monkeypatch):
    snapshot = tool_runtime.mcp_lumen.LumenSnapshot(
        grant_id="grant-opaque",
        user_id="user",
        project_id="project",
        credential_epoch=7,
        selection_generation=11,
    )
    entry = type(
        "Entry",
        (),
        {
            "name": "afterglow_vm_list",
            "description": "List current-project servers.",
            "effect": "read",
            "input_schema": lambda self: {"type": "object", "properties": {}},
        },
    )()

    async def selected(**_kwargs):
        raise AssertionError("a durable run must not reselect Lumen at worker execution")

    async def resolve(value):
        assert value == snapshot
        return type(
            "Principal",
            (),
            {"grant_id": snapshot.grant_id, "scopes": frozenset({"mcp:read"}), "service_fingerprint": "services-fingerprint"},
        )()

    monkeypatch.setattr(tool_runtime.mcp_lumen, "selected_lumen_snapshot", selected)
    monkeypatch.setattr(tool_runtime.mcp_lumen, "resolve_lumen_principal", resolve)
    monkeypatch.setattr(tool_runtime.mcp_registry, "enabled_entries", lambda _principal: (entry,))
    monkeypatch.setattr(tool_runtime.mcp_registry, "enabled_service_fingerprint", lambda _principal: "services-fingerprint")

    bindings = await tool_runtime._lumen_registry_bindings(
        ToolContext(
            project_id="project",
            user_id="user",
            lumen_snapshot=tool_runtime.mcp_lumen.snapshot_payload(snapshot),
            lumen_snapshot_frozen=True,
        )
    )

    assert len(bindings) == 1
    binding = bindings[0]
    assert binding.definition.name == "afterglow_vm_list"
    assert binding.definition.source == "managed"
    assert binding.definition.input_schema["additionalProperties"] is False
    assert binding.config_fingerprint is not None
    assert snapshot.grant_id not in binding.config_fingerprint


async def test_lumen_binding_refuses_dispatch_after_selection_changes(monkeypatch):
    snapshot = tool_runtime.mcp_lumen.LumenSnapshot(
        grant_id="grant-opaque",
        user_id="user",
        project_id="project",
        credential_epoch=7,
        selection_generation=11,
    )
    entry = type(
        "Entry",
        (),
        {
            "name": "afterglow_vm_list",
            "description": "List current-project servers.",
            "effect": "read",
            "input_schema": lambda self: {"type": "object", "properties": {}},
        },
    )()

    async def selected(**_kwargs):
        return snapshot

    calls = 0

    async def resolve(_snapshot):
        nonlocal calls
        calls += 1
        if calls == 1:
            return type(
                "Principal",
                (),
                {"grant_id": snapshot.grant_id, "scopes": frozenset({"mcp:read"}), "service_fingerprint": "services-fingerprint"},
            )()
        raise tool_runtime.mcp_lumen.McpLumenAuthorityError("selection changed")

    async def unexpected_dispatch(*_args, **_kwargs):
        raise AssertionError("a stale selection must fail before registry dispatch")

    monkeypatch.setattr(tool_runtime.mcp_lumen, "selected_lumen_snapshot", selected)
    monkeypatch.setattr(tool_runtime.mcp_lumen, "resolve_lumen_principal", resolve)
    monkeypatch.setattr(tool_runtime.mcp_registry, "enabled_entries", lambda _principal: (entry,))
    monkeypatch.setattr(tool_runtime.mcp_registry, "enabled_service_fingerprint", lambda _principal: "services-fingerprint")
    monkeypatch.setattr(tool_runtime.mcp_registry, "dispatch", unexpected_dispatch)

    binding = (await tool_runtime._lumen_registry_bindings(ToolContext(project_id="project", user_id="user")))[0]
    result = await dispatch_tool_call(binding, {}, ToolContext(project_id="project", user_id="user", run_id="run"))

    assert result.status == "failed"
    assert result.error_code == "cloud_control_unavailable"


async def test_lumen_mutation_reuses_remote_bridge_with_durable_idempotency(monkeypatch):
    snapshot = tool_runtime.mcp_lumen.LumenSnapshot(
        grant_id="grant-opaque",
        user_id="user",
        project_id="project",
        credential_epoch=7,
        selection_generation=11,
    )
    entry = type(
        "Entry",
        (),
        {
            "name": "afterglow_vm_delete",
            "description": "Delete a server.",
            "effect": "external_mutation",
            "input_schema": lambda self: {
                "type": "object",
                "properties": {"server_id": {"type": "string"}},
                "required": ["server_id"],
            },
        },
    )()
    sources: list[str] = []
    idempotency_keys: list[str] = []

    async def selected(**_kwargs):
        return snapshot

    async def resolve(value):
        assert value == snapshot
        return type(
            "Principal",
            (),
            {
                "grant_id": snapshot.grant_id,
                "scopes": frozenset({"mcp:read", "mcp:write"}),
                "service_fingerprint": "services-fingerprint",
            },
        )()

    async def claim(*_args, **kwargs):
        sources.append(kwargs["source"])
        idempotency_keys.append(kwargs["idempotency_key"])
        return tool_runtime.mcp_ledger.ClaimResult(state="replay", result={"status": "accepted"})

    args = type("Args", (), {"model_dump": lambda self, **_kwargs: {"server_id": "server-1"}})()
    monkeypatch.setattr(tool_runtime.mcp_lumen, "selected_lumen_snapshot", selected)
    monkeypatch.setattr(tool_runtime.mcp_lumen, "resolve_lumen_principal", resolve)
    monkeypatch.setattr(tool_runtime.mcp_registry, "enabled_entries", lambda _principal: (entry,))
    monkeypatch.setattr(tool_runtime.mcp_registry, "enabled_service_fingerprint", lambda _principal: "services-fingerprint")
    monkeypatch.setattr(tool_runtime.mcp_registry, "parse_entry_arguments", lambda *_args: args)
    monkeypatch.setattr(tool_runtime.mcp_ledger, "claim_mutation", claim)

    binding = (await tool_runtime._lumen_registry_bindings(ToolContext(project_id="project", user_id="user")))[0]
    result = await dispatch_tool_call(
        binding,
        {"server_id": "server-1"},
        ToolContext(project_id="project", user_id="user", run_id="run-1", tool_call_id="call-1"),
    )

    assert result.status == "completed"
    assert result.model_content == '{"status":"accepted"}'
    assert sources == ["lumen"]
    assert idempotency_keys == [tool_runtime._lumen_idempotency_key(run_id="run-1", tool_call_id="call-1")]




@pytest.mark.asyncio
async def test_lumen_binding_fingerprint_changes_with_remote_catalog(monkeypatch):
    snapshot = tool_runtime.mcp_lumen.LumenSnapshot(
        grant_id="grant-opaque",
        user_id="user",
        project_id="project",
        credential_epoch=7,
        selection_generation=11,
    )
    entry = type(
        "Entry",
        (),
        {
            "name": "afterglow_vm_delete",
            "description": "Delete a server.",
            "effect": "external_mutation",
            "input_schema": lambda self: {"type": "object", "properties": {}},
        },
    )()
    fingerprints = iter(("a" * 64, "b" * 64))

    async def selected(**_kwargs):
        return snapshot

    async def resolve(_snapshot):
        return type(
            "Principal",
            (),
            {"grant_id": snapshot.grant_id, "scopes": frozenset({"mcp:write"}), "service_fingerprint": next(fingerprints)},
        )()

    monkeypatch.setattr(tool_runtime.mcp_lumen, "selected_lumen_snapshot", selected)
    monkeypatch.setattr(tool_runtime.mcp_lumen, "resolve_lumen_principal", resolve)
    monkeypatch.setattr(tool_runtime.mcp_registry, "enabled_entries", lambda _principal: (entry,))

    first = (await tool_runtime._lumen_registry_bindings(ToolContext(project_id="project", user_id="user")))[0]
    second = (await tool_runtime._lumen_registry_bindings(ToolContext(project_id="project", user_id="user")))[0]

    assert first.config_fingerprint != second.config_fingerprint
def test_lumen_mutation_idempotency_is_unique_per_durable_tool_call():
    assert tool_runtime._lumen_idempotency_key(run_id="run-1", tool_call_id="call-1") == (
        tool_runtime._lumen_idempotency_key(run_id="run-1", tool_call_id="call-1")
    )
    assert tool_runtime._lumen_idempotency_key(run_id="run-1", tool_call_id="call-1") != (
        tool_runtime._lumen_idempotency_key(run_id="run-1", tool_call_id="call-2")
    )
