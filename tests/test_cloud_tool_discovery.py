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
        reject_execute=False,
    )

    def handle(request):
        payload = json.loads(request.content)
        state.requests.append((request.url.path, payload))
        assert request.headers["Authorization"] == f"Bearer {mcp_adapter.get_settings().lumen_mcp_service_token}"
        if request.url.path.endswith("/snapshot"):
            assert payload == {"user_id": "u1", "project_id": "p1"}
            return httpx.Response(200, json={
                "snapshot": state.snapshot,
                "entries": state.entries if state.snapshot is not None else [],
                "service_fingerprint": "a" * 64,
            })
        assert request.url.path.endswith("/execute")
        if (state.reject_execute or payload["snapshot"] != state.snapshot
                or payload["name"] not in {entry["name"] for entry in state.entries}):
            return httpx.Response(403, json={"detail": "selection unavailable"})
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
    assert all(call["snapshot"] == ctx.lumen_snapshot for call in bridge.invocations)


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
        synthetic_inference_store.authority.return_value["roles"].remove("lumen-tools_user")
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
        authority="afterglow", delegated_snapshot=ctx.lumen_snapshot,
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
