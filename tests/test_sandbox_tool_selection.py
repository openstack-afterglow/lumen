"""Explicit local-tool authority, frozen identity, and native graph wiring."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from test_tool_manifest import _sandbox_runtime

from lumen.models.chat_contracts import ChatFeatureOptions, ToolPolicy
from lumen.services import chat_admission, graph, sandbox_binding, sandbox_selection
from lumen.services.durable_runs import admission
from lumen.services.infrastructure.tool_manifest import load_tool_manifest
from lumen.services.tools import ToolContext


@pytest.fixture
def selected_runtime(tmp_path, monkeypatch):
    manifest = {"version": 1, "profiles": [{
        "id": "playwright", "command": ["/usr/bin/node", "/srv/playwright/cli.js"],
        "environment": {"PUBLIC_SETTING": "value"}, "tools": [
            {"name": name, "description": name, "input_schema": {"type": "object", "properties": {}},
             "effect": "read"} for name in ("browser_snapshot", "browser_navigate")
        ],
    }]}
    data = json.dumps(manifest).encode()
    path = tmp_path / "tools.json"
    path.write_bytes(data)
    runtime = _sandbox_runtime(path, hashlib.sha256(data).hexdigest())
    settings = SimpleNamespace(runtime_config=runtime, chat_execution_protocol_version=2)
    for module in (sandbox_selection, sandbox_binding, chat_admission):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr("lumen.config.get_settings", lambda: settings)
    return SimpleNamespace(runtime=runtime, settings=settings, path=path, manifest=manifest)


@pytest.mark.parametrize("names", [["a", "a"], [""], [" bad"], ["a-b"], ["a" * 129], [1], ["a"] * 101])
def test_tool_policy_rejects_noncanonical_selection(names):
    with pytest.raises(ValidationError):
        ToolPolicy(enabled_sandbox_tools=names)


def test_tool_policy_empty_default_and_no_implicit_all():
    assert ToolPolicy().enabled_sandbox_tools == []
    with pytest.raises(ValidationError):
        ToolPolicy(mode="none", enabled_sandbox_tools=["browser_snapshot"])


def test_empty_selection_does_not_read_runtime(monkeypatch):
    def forbidden():
        raise AssertionError("empty selection must not read runtime")
    monkeypatch.setattr(sandbox_selection, "get_settings", forbidden)
    assert sandbox_selection.freeze_selection([]) == []
    assert sandbox_selection.validate_selection([]) == []


def test_freeze_contains_only_exact_selected_secret_free_identity(selected_runtime):
    frozen = sandbox_selection.freeze_selection(["browser_snapshot"])
    assert [item["name"] for item in frozen] == ["browser_snapshot"]
    assert frozen[0]["profile"] == "playwright"
    assert set(frozen[0]) == {"name", "pool", "profile", "image", "pool_digest", "manifest_sha256"}
    assert sandbox_selection.validate_selection(frozen) == frozen
    assert sandbox_selection.selected_pool(frozen) == "browser"
    public = ChatFeatureOptions(tool_policy=ToolPolicy(enabled_sandbox_tools=["browser_snapshot"]))
    assert public.model_dump()["tool_policy"]["enabled_sandbox_tools"] == ["browser_snapshot"]
    assert "/test/token" not in json.dumps(frozen) + public.model_dump_json()
    assert "PUBLIC_SETTING" not in json.dumps(frozen)


@pytest.mark.parametrize("fault", ["image", "manifest", "profile", "pool_digest", "unknown", "disabled"])
def test_selected_identity_changes_fail_closed(selected_runtime, fault):
    frozen = sandbox_selection.freeze_selection(["browser_snapshot"])
    if fault == "disabled":
        selected_runtime.settings.runtime_config = selected_runtime.runtime.model_copy(update={"enabled": False})
    elif fault == "manifest":
        selected_runtime.path.write_text("{}")
    elif fault == "unknown":
        frozen[0]["name"] = "unknown_tool"
    else:
        frozen[0][fault] = "changed"
    with pytest.raises(sandbox_selection.SandboxSelectionError):
        sandbox_selection.validate_selection(frozen)


def test_ambiguous_manifest_names_never_choose_arbitrary_pool(selected_runtime):
    runtime = selected_runtime.runtime
    twin = runtime.pools[0].model_copy(update={"name": "other"})
    with pytest.raises(sandbox_selection.SandboxSelectionError, match="sandbox_selection_ambiguous"):
        sandbox_selection.freeze_selection(["browser_snapshot"], config=runtime.model_copy(update={"pools": (*runtime.pools, twin)}))


def test_admission_checks_selection_and_protocol_before_resources(selected_runtime, monkeypatch):
    frozen = sandbox_selection.freeze_selection(["browser_snapshot"])
    payload = {"features": {"tool_policy": {"enabled_sandbox_tools": ["browser_snapshot"]}}}
    admission._validate_sandbox_snapshot(payload, {"sandbox_tools": frozen}, 2)
    with pytest.raises(admission.DurableRunInputError, match="sandbox_selection_invalid"):
        admission._validate_sandbox_snapshot(payload, {}, 2)
    with pytest.raises(admission.DurableRunInputError, match="sandbox_tool_unavailable"):
        admission._validate_sandbox_snapshot(payload, {"sandbox_tools": frozen}, 1)
    from lumen.services.checkpointer import chat_checkpointer
    monkeypatch.setattr(type(chat_checkpointer), "available", property(lambda self: True))
    features = ChatFeatureOptions.model_validate(payload["features"])
    assert chat_admission._freeze_sandbox_tools(features) == frozen
    selected_runtime.settings.chat_execution_protocol_version = 1
    with pytest.raises(HTTPException) as exc:
        chat_admission._freeze_sandbox_tools(features)
    assert exc.value.status_code == 503
    assert exc.value.detail == "sandbox_tool_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [False, True])
async def test_graph_binds_only_explicit_selection(selected_runtime, monkeypatch, selected):
    observed = {}
    async def initial(_ctx):
        return {}
    class Graph:
        async def astream(self, *args, **kwargs):
            if False:
                yield None
    def build(params, _ctx):
        observed.update(params["v2_bindings"])
        return Graph()
    monkeypatch.setattr(graph.bindings, "v2_tool_bindings", initial)
    monkeypatch.setattr(graph, "_build_graph", build)
    frozen = sandbox_selection.freeze_selection(["browser_snapshot"]) if selected else []
    assert [event async for event in graph.stream(
        model="fixture", messages=[], project_id="project", user_id="owner", run_id="run",
        execution_protocol_version=2, sandbox_lease=("worker#1", 1), sandbox_tools=frozen,
    )] == []
    assert set(observed) == ({"browser_snapshot"} if selected else set())


@pytest.mark.asyncio
async def test_unselected_binding_cannot_dispatch_even_with_same_profile(selected_runtime, monkeypatch):
    calls = []
    async def forbidden(**kwargs):
        calls.append(kwargs)
        raise AssertionError("unselected tool dispatched")
    monkeypatch.setattr(sandbox_binding, "execute_mcp_in_sandbox", forbidden)
    frozen = sandbox_selection.freeze_selection(["browser_snapshot"])
    context = ToolContext(project_id="project", user_id="owner", run_id="run", tool_call_id="call")
    definition = load_tool_manifest(selected_runtime.path, frozen[0]["manifest_sha256"])[0].tools[1]
    binding = sandbox_binding.kubernetes_mcp_binding(
        context, lease_owner="worker#1", fence=1, profile_id="playwright", pool_name="browser",
        tool_def=definition, sandbox_tools=frozen,
    )
    result = await binding.execute({}, context)
    assert result.error_code == "sandbox_tool_unselected"
    assert not calls


@pytest.mark.asyncio
async def test_resumed_binding_revalidates_manifest_bytes_before_dispatch(selected_runtime, monkeypatch):
    frozen = sandbox_selection.freeze_selection(["browser_snapshot"])
    context = ToolContext(project_id="project", user_id="owner", run_id="run", tool_call_id="call")
    definition = load_tool_manifest(selected_runtime.path, frozen[0]["manifest_sha256"])[0].tools[0]
    binding = sandbox_binding.kubernetes_mcp_binding(
        context, lease_owner="worker#2", fence=2, profile_id="playwright", pool_name="browser",
        tool_def=definition, sandbox_tools=copy.deepcopy(frozen),
    )
    selected_runtime.path.write_text("{}")
    result = await binding.execute({}, context)
    assert result.error_code == "sandbox_tool_unavailable"


@pytest.mark.asyncio
async def test_code_with_selected_tools_is_rejected_before_native_admission_work(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("unsupported code selection must fail before prerequisite or provider work")

    monkeypatch.setattr(chat_admission, "_agent_runtime_prerequisites", forbidden)
    monkeypatch.setattr(chat_admission, "_freeze_sandbox_tools", forbidden)
    features = ChatFeatureOptions(tool_policy=ToolPolicy(enabled_sandbox_tools=["browser_snapshot"]))
    with pytest.raises(HTTPException) as exc:
        await chat_admission.prepare_context_input(
            user_id="owner", project_id="project", model_id="fixture", features=features,
            execution_mode="code",
        )
    assert exc.value.status_code == 422
    assert exc.value.detail == "sandbox_code_combination_unsupported"


def test_durable_code_selection_guard_precedes_manifest_and_preserves_empty_nova(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("unsupported combination must fail before manifest lookup")

    payload = {"features": {"tool_policy": {"enabled_sandbox_tools": ["browser_snapshot"]}}}
    with monkeypatch.context() as patch:
        patch.setattr(sandbox_selection, "freeze_selection", forbidden)
        with pytest.raises(admission.DurableRunInputError, match="^sandbox_code_combination_unsupported$"):
            admission._validate_sandbox_snapshot(payload, {}, 2, execution_mode="code")
    admission._validate_sandbox_snapshot({"features": {"tool_policy": {}}}, {}, 2, execution_mode="code")
