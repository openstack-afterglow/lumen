"""Native chat consumers of delegated cloud discovery; no provider or cloud I/O.

Only the HTTP authority boundary is synthetic. Admission snapshot parsing,
search/load, schema projection, current tool permission and bridge dispatch are real.
"""
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from lumen_plugin_api.contracts import Namespace, PluginError
from lumen_plugin_api.mcp import McpSelection

from lumen.services import mcp_adapter
from lumen.services.api_key_store import ApiKeyForbidden
from lumen.services.durable_runs.common import _freeze_lumen_snapshot
from lumen.services.tool_runtime import bindings, contracts, dispatch, selection
from lumen.services.tools import ToolContext

pytestmark = pytest.mark.usefixtures("synthetic_inference_store")


def _entry(name, description, effect="read"):
    return {
        "name": name,
        "description": description,
        "effect": effect,
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    }


@pytest.fixture
def bridge(monkeypatch):
    snapshot = {
        "grant_id": "delegated-grant",
        "user_id": "u1",
        "project_id": "p1",
        "credential_epoch": 3,
        "selection_generation": 7,
    }
    state = SimpleNamespace(
        snapshot=snapshot,
        entries=[
            _entry("afterglow_vm_list", "List bounded safe metadata for Nova servers owned by the current project."),
            _entry("afterglow_volume_list", "List bounded safe metadata for Cinder volumes owned by the current project."),
            _entry("afterglow_network_list", "List bounded current-project and explicitly shared Neutron network metadata."),
            _entry("afterglow_vm_delete", "Delete one current-project Nova server.", "external_mutation"),
        ],
        requests=[],
        invocations=[],
        previews=[],
        reject_execute=False,
        execute_failure_status=403,
    )

    def handle(request):
        payload = json.loads(request.content)
        state.requests.append((request.url.path, payload))
        assert request.headers["Authorization"] == f"Bearer {mcp_adapter.get_settings().lumen_mcp_service_token}"
        if request.url.path.endswith("/snapshot"):
            assert payload == {"user_id": "u1", "project_id": "p1"}
            view = {"snapshot": state.snapshot, "entries": state.entries, "service_fingerprint": "a" * 64}
            return httpx.Response(200, json={
                "snapshots": [view] if state.snapshot is not None else [],
                "snapshot": state.snapshot,
                "entries": state.entries if state.snapshot is not None else [],
                "service_fingerprint": "a" * 64,
            })
        if request.url.path.endswith("/preview"):
            if payload["snapshot"] != state.snapshot:
                return httpx.Response(403, json={"detail": "selection unavailable"})
            state.previews.append(payload)
            arguments = payload["arguments"]
            return httpx.Response(200, json={
                "resource_identity": f"nova-server:new:p1:{arguments['name']}",
                "current_state": None,
                "intended_transition": "create",
                "dependent_resources": [
                    f"glance-image:{arguments['image_id']}",
                    f"nova-flavor:{arguments['flavor_id']}",
                    f"neutron-network:{arguments['network_id']}",
                    f"nova-keypair:u1:{arguments['key_name']}",
                    *[f"neutron-security-group:{group}" for group in arguments["security_group_ids"]],
                ],
                "destructive": False,
                "estimated_effect": f"create one VM with a {arguments['boot_volume_size_gb']} GiB boot volume",
                "fingerprint": "b" * 64,
            })
        assert request.url.path.endswith("/execute")
        if (state.reject_execute or payload["snapshot"] != state.snapshot
                or payload["name"] not in {entry["name"] for entry in state.entries}):
            return httpx.Response(state.execute_failure_status, json={"detail": "selection unavailable"})
        state.invocations.append(payload)
        return httpx.Response(200, json={"tool": payload["name"], "fixture": "read-result"})

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handle)

    def client(**kwargs):
        return original_client(transport=transport, **kwargs)

    monkeypatch.setattr(mcp_adapter.httpx, "AsyncClient", client)
    monkeypatch.setattr(mcp_adapter, "get_settings", lambda: SimpleNamespace(
        mcp_control_plane_url="https://authority.example.test",
        lumen_mcp_service_token="synthetic-service-token",
        verify=True,
    ))

    async def no_extensions(_context):
        return []

    monkeypatch.setattr(bindings, "_load_custom", no_extensions)
    monkeypatch.setattr(bindings, "_load_mcp", no_extensions)
    return state


async def _admitted_context(protocol=1, *, tools_enabled=True):
    payload = await _freeze_lumen_snapshot(
        {"execution_protocol_version": protocol}, user_id="u1", project_id="p1",
    )
    return ToolContext(
        user_id="u1", project_id="p1", run_id="synthetic-run",
        tools_enabled=tools_enabled,
        lumen_snapshot=payload["lumen_snapshot"], lumen_snapshot_frozen=True,
        binding_session=contracts.ToolBindingSession(),
    )


async def _search(ctx, query, protocol=1):
    if protocol == 1:
        result = await dispatch.context_execute_result(
            "list_available_tools", {"query": query, "max_results": 8}, ctx,
        )
        return json.loads(result.content), ctx.binding_session.legacy_bindings
    target = await bindings.v2_tool_bindings(ctx)
    result = await target["list_available_tools"].execute({"query": query, "max_results": 8}, ctx)
    return json.loads(result.model_content), target


@pytest.mark.parametrize("protocol", [1, 2])
@pytest.mark.parametrize("query", [
    "현재 프로젝트의 컴퓨트/스토리지/네트워크 리소스를 읽기 전용으로 요약",
    "현재 프로젝트의 컴퓨팅과 스토리지 및 네트워크 조회",
    "클라우드 인벤토리",
    "Nova Cinder Neutron inventory",
])
async def test_active_grant_search_load_and_actual_bridge_dispatch(bridge, protocol, query):
    ctx = await _admitted_context(protocol)
    if protocol == 1:
        initial = {schema["function"]["name"] for schema in await selection.context_tool_schemas(ctx)}
        assert initial == {"list_my_conversations", "get_conversation_detail", "list_available_tools"}
    loaded, target = await _search(ctx, query, protocol)
    expected = {"afterglow_vm_list", "afterglow_volume_list", "afterglow_network_list"}
    assert expected <= set(loaded["loaded_tools"])
    assert all(item["name"] in target for item in loaded["tools"])
    if protocol == 1:
        schemas = {schema["function"]["name"] for schema in await selection.context_tool_schemas(ctx)}
        assert expected <= schemas
        assert "afterglow_vm_delete" not in schemas
        for name in sorted(expected):
            result = await dispatch.context_execute_result(name, {}, ctx)
            assert result.status == "completed"
    else:
        for name in sorted(expected):
            result = await target[name].execute({}, ctx)
            assert result.status == "completed"
    assert {call["name"] for call in bridge.invocations} == expected
    assert all(call["snapshot"] == ctx.lumen_snapshot[0] for call in bridge.invocations)


@pytest.mark.parametrize(("query", "expected"), [
    ("컴퓨트 조회", "afterglow_vm_list"),
    ("스토리지의 볼륨 조회", "afterglow_volume_list"),
    ("네트워크를 조회", "afterglow_network_list"),
])
async def test_korean_capability_search_is_relevant_not_all_tools(bridge, query, expected):
    ctx = await _admitted_context()
    loaded, _ = await _search(ctx, query)
    assert loaded["loaded_tools"] == [expected]
    unrelated, _ = await _search(ctx, "weather forecast")
    assert unrelated["tools"] == []


@pytest.mark.parametrize(("field", "value"), [
    ("selection_generation", 8), ("credential_epoch", 4), ("grant_id", "other-grant"),
    ("user_id", "foreign-user"), ("project_id", "foreign-project"),
    (None, None),
])
async def test_changed_or_disabled_grant_rejected_at_search_dispatch_and_replay(bridge, field, value):
    ctx = await _admitted_context()
    loaded, _ = await _search(ctx, "컴퓨트")
    assert loaded["loaded_tools"] == ["afterglow_vm_list"]
    bridge.snapshot = {**bridge.snapshot, field: value} if field is not None else None
    result = await dispatch.context_execute_result("afterglow_vm_list", {}, ctx)
    assert result.status == "failed"
    assert result.warning_code == "cloud_control_unavailable"
    assert bridge.invocations == []
    loaded, _ = await _search(ctx, "클라우드")
    assert loaded["tools"] == []
    resumed = replace(ctx, binding_session=contracts.ToolBindingSession())
    with pytest.raises(RuntimeError, match="no longer eligible"):
        await bindings.restore_v2_deferred_bindings(
            resumed, resumed.binding_session.legacy_bindings, ["afterglow_vm_list"], include_managed=False,
        )


async def test_loaded_read_replays_only_under_same_frozen_authority(bridge):
    ctx = await _admitted_context()
    loaded, _ = await _search(ctx, "컴퓨트")
    resumed = replace(ctx, binding_session=contracts.ToolBindingSession())
    await bindings.restore_v2_deferred_bindings(
        resumed, resumed.binding_session.legacy_bindings, loaded["loaded_tools"], include_managed=False,
    )
    result = await dispatch.context_execute_result("afterglow_vm_list", {}, resumed)
    assert result.status == "completed"
    assert len(bridge.invocations) == 1


async def test_frozen_absent_grant_cannot_acquire_later_selection(bridge):
    active = bridge.snapshot
    bridge.snapshot = None
    ctx = await _admitted_context()
    bridge.snapshot = active
    loaded, _ = await _search(ctx, "클라우드")
    assert loaded["tools"] == []
    assert len(bridge.requests) == 1  # admission only; no late authority lookup


async def test_bridge_rejection_after_local_revalidation_does_not_dispatch(bridge):
    ctx = await _admitted_context()
    await _search(ctx, "컴퓨트")
    bridge.reject_execute = True
    result = await dispatch.context_execute_result("afterglow_vm_list", {}, ctx)
    assert result.status == "failed"
    assert bridge.requests[-1][0].endswith("/execute")
    assert bridge.invocations == []


@pytest.mark.parametrize("authority", ["key", "owner"])
async def test_current_tool_permission_loss_blocks_loaded_cloud_invocation(
    bridge, synthetic_inference_store, authority,
):
    ctx = await _admitted_context()
    await _search(ctx, "컴퓨트")
    if authority == "key":
        synthetic_inference_store.key.scopes.remove("native:tools:execute")
    else:
        synthetic_inference_store.authority.return_value["roles"] = ["reader"]
    request_count = len(bridge.requests)
    with pytest.raises(ApiKeyForbidden):
        await dispatch.context_execute_result("afterglow_vm_list", {}, ctx)
    assert len(bridge.requests) == request_count
    assert bridge.invocations == []


async def test_run_tool_policy_disabled_exposes_no_tools(bridge):
    ctx = await _admitted_context(tools_enabled=False)
    assert await selection.context_tool_schemas(ctx) == []
    assert await bindings.v2_tool_bindings(ctx, include_managed=True) == {}
    result = await dispatch.context_execute_result("list_available_tools", {"query": "클라우드"}, ctx)
    assert "disabled" in result.content
    assert bridge.invocations == []


async def test_installed_afterglow_plugin_uses_bridge_and_revalidates_authority(bridge):
    from lumen.plugins.registry import get_plugin

    ctx = await _admitted_context()
    plugin = get_plugin("mcp", "afterglow-mcp")
    snapshots = await plugin.describe(McpSelection(
        namespace=Namespace(user_id=ctx.user_id, project_id=ctx.project_id),
        authority="afterglow", delegated_snapshot=ctx.lumen_snapshot[0],
    ))
    assert len(snapshots) == 1
    bound = await plugin.bind(snapshots[0], contracts.execution_context(ctx))
    read = next(binding for binding in bound if binding.definition.name == "afterglow_vm_list")
    result = await read.execute({}, contracts.execution_context(ctx))
    assert json.loads(result.model_content) == {"tool": "afterglow_vm_list", "fixture": "read-result"}
    assert len(bridge.invocations) == 1
    bridge.snapshot = None
    with pytest.raises(PluginError):
        await read.execute({}, contracts.execution_context(ctx))
    assert len(bridge.invocations) == 1


async def test_legacy_chat_never_dispatches_delegated_cloud_mutations(bridge):
    ctx = await _admitted_context()
    loaded, _ = await _search(ctx, "Delete Nova server")
    assert "afterglow_vm_delete" not in loaded["loaded_tools"]
    result = await dispatch.context_execute_result("afterglow_vm_delete", {}, ctx)
    assert result.content == "알 수 없는 툴입니다: afterglow_vm_delete"
    assert bridge.invocations == []
    assert not any(path.endswith("/execute") for path, _ in bridge.requests)


@pytest.mark.parametrize("protocol", [1, 2])
@pytest.mark.parametrize("upstream_status", [403, 502])
async def test_failed_port_list_journals_valid_result_and_reaches_next_model_turn(
    monkeypatch, bridge, protocol, upstream_status,
):
    from unittest.mock import AsyncMock

    from langgraph.checkpoint.memory import MemorySaver

    from lumen import crypto
    from lumen.models.chat_runs import ChatRunSegment
    from lumen.services import graph
    from lumen.services.durable_runs import common, execution
    from lumen.services.run_store import complete_segment_io, load_segment_payload

    bridge.entries = [_entry("afterglow_port_list", "List project-owned Neutron ports.")]
    ctx = await _admitted_context(protocol)
    bound, = await bindings._lumen_registry_bindings(ctx)
    bridge.reject_execute = True
    bridge.execute_failure_status = upstream_status
    hooks = execution._DurableExecutionHooks(run_id="port-failure", owner="worker#1")
    journal = []
    segments = []
    provider_messages = []
    retained = None

    async def loaded_bindings(_context, **_kwargs):
        return {"afterglow_port_list": bound}

    async def schemas(_context):
        return [{
            "type": "function",
            "function": {"name": bound.definition.name, "description": bound.definition.description,
                         "parameters": bound.definition.input_schema},
        }]

    async def start(**kwargs):
        if kwargs["endpoint"] == "tool" and retained is not None:
            return {**retained, "_durable_replay": True}
        return None

    async def complete(**kwargs):
        nonlocal retained
        if kwargs["endpoint"] != "tool":
            return
        segment = ChatRunSegment(endpoint="tool", status="provider_started")
        complete_segment_io(segment, result_payload=kwargs["result_payload"], usage_payload=None)
        retained = load_segment_payload(segment.result_payload)
        segments.append(segment)
        event_type, payload = kwargs["journal_completed"]
        journal.append(common._event(SimpleNamespace(id="port-failure", last_seq=len(journal)), event_type, payload))

    async def completion_stream(**kwargs):
        provider_messages.append(kwargs["messages"])
        first_turn = len(provider_messages) % 2 == 1

        async def chunks():
            if first_turn:
                yield {"choices": [{"delta": {"tool_calls": [{
                    "index": 0, "id": "port-call", "function": {"name": "afterglow_port_list", "arguments": "{}"},
                }]}}]}
            else:
                yield {"choices": [{"delta": {"content": "The tool could not list ports."}}]}
            yield {"usage": {"prompt_tokens": 3, "completion_tokens": 1}}

        return chunks()

    monkeypatch.setattr(crypto, "_get_key", lambda: bytes.fromhex("ab" * 32))
    monkeypatch.setattr(hooks, "_start", start)
    monkeypatch.setattr(hooks, "_complete", complete)
    monkeypatch.setattr(hooks, "prepare_context", AsyncMock(return_value=None))
    monkeypatch.setattr(hooks, "loaded_tool_names", AsyncMock(return_value=None))
    monkeypatch.setattr(bindings, "v2_tool_bindings", loaded_bindings)
    monkeypatch.setattr(bindings, "_legacy_dynamic_bindings", loaded_bindings)
    monkeypatch.setattr(selection, "context_tool_schemas", schemas)
    monkeypatch.setattr(graph.provider_client, "acompletion_stream", completion_stream)
    monkeypatch.setattr(graph.chat_checkpointer, "_saver", MemorySaver())

    for run_id in ("port-failure", "port-failure-replay"):
        events = [event async for event in graph.stream(
            model="test-model", messages=[{"role": "user", "content": "list ports"}],
            project_id="p1", user_id="u1", execution_protocol_version=protocol, execution_hooks=hooks,
            run_id=run_id, lumen_snapshot=ctx.lumen_snapshot, lumen_snapshot_frozen=True,
        )]
        result = next(event for event in events if event["type"] == "tool_result")
        assert result["status"] == "failed"
        assert result["error_code"] == "cloud_control_unavailable"
        assert result["hidden"] is True
        assert result["content"] == ""
        assert result["_durable_journaled"] is True
        assert any(event.get("text") == "The tool could not list ports." for event in events)
        assert not any(event["type"] == "error" for event in events)
        assert provider_messages[-1][-1]["content"] == "Cloud control-plane call was denied or is unavailable."

    assert len(journal) == len(segments) == 1
    assert segments[0].status == "completed"
    assert journal[0].payload.status == "failed"
    assert journal[0].payload.error_code == "cloud_control_unavailable"
    assert journal[0].payload.content[0].text == "Tool call failed. Output is hidden."
    assert retained["content"] == "Cloud control-plane call was denied or is unavailable."
    assert retained["visible"] is False
    assert len([path for path, _payload in bridge.requests if path.endswith("/execute")]) == 1


@pytest.mark.parametrize("protocol", [1, 2])
async def test_cloud_creation_discovery_explains_consent_and_runtime(bridge, protocol):
    ctx = await _admitted_context(protocol)
    loaded, _ = await _search(ctx, "인스턴스를 만들어 줘", protocol)
    access = loaded["cloud_access"]
    assert access["mutation_protocol_version"] == 2
    assert access["runtime_supports_mutations"] is (protocol == 2)
    assert access["consent_url"] == "/dashboard/chat/settings?section=mcp"
    assert "owner/project write consent" in access["instructions"]
    assert "boot_volume_size_gb" in access["vm_creation"]
    assert access["write_consent_required"] is (False if protocol == 2 else None)
    if protocol == 2:
        assert access["managed_mutation_eligible"] is True
        assert "do not deactivate or re-consent" in access["instructions"]
        assert "choose Read and manage" not in access["instructions"]
    else:
        assert "operator" in access["instructions"] and "first" in access["instructions"]
        assert "choose Read and manage" not in access["instructions"]
    assert "afterglow_vm_list" in loaded["loaded_tools"]
    assert bridge.invocations == []


async def test_v2_read_consent_does_not_acquire_creation_authority(bridge):
    bridge.entries = [entry for entry in bridge.entries if entry["effect"] == "read"]
    ctx = await _admitted_context(2)
    loaded, target = await _search(ctx, "인스턴스 생성", 2)
    assert "afterglow_vm_create" not in loaded["loaded_tools"]
    assert "afterglow_vm_create" not in target
    assert loaded["cloud_access"]["runtime_supports_mutations"] is True
    assert loaded["cloud_access"]["consent_url"] == "/dashboard/chat/settings?section=mcp"
    assert loaded["cloud_access"]["write_consent_required"] is True
    assert loaded["cloud_access"]["managed_mutation_eligible"] is False
    assert "choose Read and manage" in loaded["cloud_access"]["instructions"]
    assert bridge.previews == bridge.invocations == []


async def test_v2_manage_consent_guidance_uses_all_eligible_bindings_not_only_search_hits(bridge):
    ctx = await _admitted_context(2)
    target = await bindings.v2_tool_bindings(ctx)
    result = await target["list_available_tools"].execute({"query": "Nova", "max_results": 1}, ctx)
    loaded = json.loads(result.model_content)
    assert loaded["loaded_tools"] == ["afterglow_vm_list"]
    assert loaded["cloud_access"]["write_consent_required"] is False
    assert loaded["cloud_access"]["managed_mutation_eligible"] is True
    assert "choose Read and manage" not in loaded["cloud_access"]["instructions"]


@pytest.mark.parametrize("approval_mode", ["required_for_mutations", "never"])
@pytest.mark.parametrize("decision", ["approve", "deny", "revoked", "changed_epoch"])
async def test_vm_creation_chat_requires_exact_human_approval(
    monkeypatch, bridge, approval_mode, decision,
):
    """Synthetic model + HTTP authority: real discovery, graph interrupt and dispatch."""
    from langgraph.checkpoint.memory import MemorySaver

    from lumen.services import graph

    arguments = {
        "name": "chat-vm",
        "image_id": "11111111-1111-4111-8111-111111111111",
        "flavor_id": "small",
        "network_id": "22222222-2222-4222-8222-222222222222",
        "boot_volume_size_gb": 24,
        "key_name": "my-key",
        "security_group_ids": ["33333333-3333-4333-8333-333333333333"],
    }
    create = _entry(
        "afterglow_vm_create", "Create one Nova VM with explicit selections.", "external_mutation",
    )
    create["input_schema"] = {
        "type": "object",
        "properties": {
            name: {"type": "integer", "minimum": 1} if name == "boot_volume_size_gb"
            else {"type": "array", "items": {"type": "string"}, "minItems": 1} if name == "security_group_ids"
            else {"type": "string", "minLength": 1}
            for name in arguments
        },
        "required": list(arguments),
        "additionalProperties": False,
    }
    reads = [
        "afterglow_image_list", "afterglow_flavor_list", "afterglow_network_list",
        "afterglow_keypair_list", "afterglow_security_group_list", "afterglow_quota_get",
    ]
    bridge.entries = [create, *[_entry(name, f"Read available choices: {name}.") for name in reads]]
    ctx = await _admitted_context(2)
    loaded_names = []
    provider_schemas = []
    provider_messages = []

    class Hooks:
        async def loaded_tool_names(self):
            return list(loaded_names)

        async def tool_completed(self, *, tool_name, result_payload, **_kwargs):
            if tool_name == "list_available_tools":
                for name in json.loads(result_payload["content"])["loaded_tools"]:
                    if name not in loaded_names:
                        loaded_names.append(name)

    async def no_builtins(_context):
        return {}

    async def completion_stream(**kwargs):
        provider_messages.append(kwargs["messages"])
        provider_schemas.append({item["function"]["name"] for item in kwargs.get("tools") or []})
        turn = len(provider_messages)

        async def chunks():
            if turn == 1:
                calls = [("discover-create", "list_available_tools", {"query": "인스턴스를 만들어 줘"})]
            elif turn == 2:
                calls = [("discover-choices", "list_available_tools", {
                    "query": "image flavor network keypair security group quota", "max_results": 8,
                })]
            elif turn == 3:
                calls = [(f"read-{index}", name, {}) for index, name in enumerate(reads)]
            elif turn == 4:
                calls = [("create-vm", "afterglow_vm_create", arguments)]
            else:
                calls = []
            if calls:
                yield {"choices": [{"delta": {"tool_calls": [
                    {"index": index, "id": call_id, "function": {
                        "name": name, "arguments": json.dumps(args),
                    }}
                    for index, (call_id, name, args) in enumerate(calls)
                ]}}]}
            else:
                yield {"choices": [{"delta": {"content": "VM request handled; guest readiness is not confirmed."}}]}
            yield {"usage": {"prompt_tokens": 3, "completion_tokens": 1}}

        return chunks()

    monkeypatch.setattr(bindings, "v2_builtin_tool_bindings", no_builtins)
    monkeypatch.setattr(graph.provider_client, "acompletion_stream", completion_stream)
    monkeypatch.setattr(graph.chat_checkpointer, "_saver", MemorySaver())
    parameters = {
        "model": "synthetic-model", "messages": [{"role": "user", "content": "인스턴스를 만들어 줘"}],
        "project_id": "p1", "user_id": "u1", "execution_protocol_version": 2,
        "approval_mode": approval_mode, "execution_hooks": Hooks(),
        "run_id": f"vm-create-{decision}-{approval_mode}",
        "lumen_snapshot": ctx.lumen_snapshot, "lumen_snapshot_frozen": True,
    }
    events = [event async for event in graph.stream(**parameters)]
    approval = next(event["payload"] for event in events if event.get("type") == "input.interrupted")
    call, = approval["calls"]
    assert call["call_id"] == "create-vm"
    assert call["effect"] == "external_mutation"
    assert json.loads(call["arguments"]) == arguments
    preview_text = "\n".join(part["text"] for part in call["preview"])
    assert "nova-server:new:p1:chat-vm" in preview_text
    assert "24 GiB" in preview_text
    for field in ("image_id", "flavor_id", "network_id", "key_name"):
        assert arguments[field] in preview_text
    assert arguments["security_group_ids"][0] in preview_text
    assert provider_schemas[0] == {"list_available_tools"}
    assert "afterglow_vm_create" in provider_schemas[1]
    assert set(reads) <= provider_schemas[2]
    assert {item["name"] for item in bridge.invocations} == set(reads)
    assert len(bridge.previews) == 1
    assert bridge.previews[0]["arguments"] == arguments
    assert not any(item["name"] == "afterglow_vm_create" for item in bridge.invocations)

    if decision == "revoked":
        bridge.snapshot = None
    elif decision == "changed_epoch":
        bridge.snapshot = {**bridge.snapshot, "credential_epoch": 4}
    resume = {"kind": "tool_approval", "decisions": [
        {"call_id": "create-vm", "decision": "deny" if decision == "deny" else "approve"},
    ]}
    if decision in {"revoked", "changed_epoch"}:
        with pytest.raises(RuntimeError):
            _ = [event async for event in graph.stream(**parameters, resume=resume)]
    else:
        resumed = [event async for event in graph.stream(**parameters, resume=resume)]
        result = next(event for event in resumed if event.get("type") == "tool_result")
        assert result["status"] == ("completed" if decision == "approve" else "failed")
        if decision == "deny":
            assert result["error_code"] == "tool_denied"
        assert len(provider_messages) == 5

    mutations = [item for item in bridge.invocations if item["name"] == "afterglow_vm_create"]
    assert len(mutations) == (1 if decision == "approve" else 0)
    if mutations:
        assert mutations[0]["arguments"] == arguments
        assert mutations[0]["snapshot"] == ctx.lumen_snapshot[0]
        assert mutations[0]["idempotency_key"]
