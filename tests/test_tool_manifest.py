"""Pinned worker definitions: local-only tests, no Kubernetes/network evidence."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from lumen_plugin_api.tools import ToolDefinition

from lumen.services.infrastructure.tool_manifest import (
    MAX_TOOL_MANIFEST_BYTES,
    ToolManifestError,
    load_tool_manifest,
    parse_tool_manifest,
    validate_worker_tool_manifests,
)


@pytest.fixture
def manifest():
    return {
        "version": 1,
        "profiles": [{
            "id": "playwright",
            "command": ["/usr/bin/node", "/srv/playwright/cli.js"],
            "environment": {"NODE_ENV": "production"},
            "tools": [{
                "name": "browser_navigate",
                "description": "Navigate the isolated browser.",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
                "effect": "external_mutation",
            }],
        }],
    }


def _bytes(manifest):
    return json.dumps(manifest, separators=(",", ":")).encode()


def _pin(data):
    return hashlib.sha256(data).hexdigest()


def _pool(path, pin, *, enabled=True, backend="kubernetes", role="sandbox"):
    return SimpleNamespace(
        name="browser", enabled=enabled, backend=backend, role=role,
        profile=SimpleNamespace(tool_manifest_file=str(path), tool_manifest_sha256=pin),
    )


def test_loader_returns_typed_definitions(tmp_path, manifest):
    data = _bytes(manifest)
    path = tmp_path / "tools.json"
    path.write_bytes(data)
    profiles = load_tool_manifest(path, _pin(data))
    assert len(profiles) == 1
    assert profiles[0].id == "playwright"
    assert profiles[0].command == ("/usr/bin/node", "/srv/playwright/cli.js")
    assert profiles[0].environment == {"NODE_ENV": "production"}
    definition = profiles[0].tools[0]
    assert isinstance(definition, ToolDefinition)
    assert definition.name == "browser_navigate"
    assert definition.effect == "external_mutation"
    assert definition.source == "workspace"


def test_loader_preserves_open_server_schemas_without_relaxing_plugin_policy(manifest):
    schema = {"type": "object", "properties": {"nested": {"type": "object"}}}
    manifest["profiles"][0]["tools"][0]["input_schema"] = schema
    data = _bytes(manifest)
    definition = parse_tool_manifest(data, _pin(data))[0].tools[0]
    assert definition.input_schema == schema
    with pytest.raises(ValueError, match="additionalProperties"):
        ToolDefinition.model_validate(definition.model_dump())


@pytest.mark.parametrize("fault", ["bytes", "depth", "properties"])
def test_pinned_schema_retains_core_resource_limits(manifest, fault):
    schema = {"type": "object"}
    if fault == "bytes":
        schema["description"] = "a" * (32 * 1024)
    elif fault == "properties":
        schema["properties"] = {f"field_{index}": {"type": "string"} for index in range(101)}
    else:
        for _ in range(17):
            schema = {"type": "object", "properties": {"nested": schema}}
    manifest["profiles"][0]["tools"][0]["input_schema"] = schema
    data = _bytes(manifest)
    with pytest.raises(ToolManifestError):
        parse_tool_manifest(data, _pin(data))


def _sandbox_runtime(path, pin):
    from lumen.services.infrastructure.config import RuntimeConfig

    return RuntimeConfig.model_validate_json(json.dumps({
        "enabled": True, "controller_url": "https://192.0.2.11:8013",
        "tls": {"ca_file": "/test/ca.pem", "ca_key_file": "/test/ca.key",
                "cert_file": "/test/server.pem", "key_file": "/test/server.key",
                "operator_client_cert_file": "/test/probe.pem", "operator_client_key_file": "/test/probe.key"},
        "dispatch_key": {"file": "/test/dispatch.key"}, "managed_networks": ["192.0.2.0/24"],
        "kubernetes_clusters": [{"id": "cluster", "server": "https://192.0.2.1:6443", "ca_file": "/test/kube-ca.pem",
                                 "bearer_token": {"file": "/test/token"}, "namespace": "runs",
                                 "runtime_class_name": "gvisor", "runtime_class_handler": "runsc",
                                 "cluster_cidrs": ["10.42.0.0/16"], "qualification_file": "/test/qualification.json",
                                 "qualification_sha256": "c" * 64}],
        "pools": [{"name": "browser", "role": "sandbox", "backend": "kubernetes", "enabled": True,
                   "image": "registry.example/sandbox@sha256:" + "a" * 64, "architecture": "x86_64",
                   "min_replicas": 0, "max_replicas": 2,
                   "sandbox": {"workspace_bytes": 128 * 1024**2, "memory_bytes": 1024**3,
                               "cpu_millis": 1000, "pids_limit": 256},
                   "profile": {"backend": "kubernetes", "cluster_profile_id": "cluster",
                               "tool_manifest_file": str(path), "tool_manifest_sha256": pin,
                               "controller_ingress_cidrs": ["192.0.2.11/32"],
                               "bootstrap_egress_cidrs": ["192.0.2.11/32"],
                               "egress_deny_cidrs": ["198.51.100.0/24"]}}],
    }))


@pytest.mark.parametrize("text", ["a" * 9000, "a" * 60_000, "a" * 100_001, "한" * 30_000, "\x00" * 20_000],
                         ids=["model-bound", "checkpoint-bound", "large-text", "unicode", "json-escaped"])
def test_mcp_large_text_retains_full_output_and_durable_checkpoint(monkeypatch, text):
    from lumen import crypto
    from lumen.models.chat_runs import ChatRunSegment
    from lumen.services.run_store import complete_segment_io, load_segment_payload
    from lumen.services.sandbox_binding import _project_mcp_result

    monkeypatch.setattr(crypto, "_get_key", lambda: bytes.fromhex("ab" * 32))
    result = _project_mcp_result({"state": "succeeded", "mcp_result": {"content": [{"type": "text", "text": text}]}})
    assert result.status == "completed"
    assert len(result.model_content) <= 8192
    assert result.model_content.endswith("[Truncated; see displayed result or generated files.]")
    if text == "a" * 9000:
        assert result.display[0].text == text and not result.generated_files
    else:
        assert result.generated_files[0].data == text.encode()
        assert result.display[0].text.endswith("[Truncated; see displayed result or generated files.]")
    payload = {"content": result.model_content, "display": [part.model_dump(mode="json") for part in result.display],
               "tool_name": "read_file", "usage": [], "visible": True, "artifacts": [],
               "warning_code": None, "status": result.status, "error_code": None}
    segment = ChatRunSegment(endpoint="tool", status="provider_started")
    complete_segment_io(segment, result_payload=payload, usage_payload=None)
    assert segment.status == "completed" and load_segment_payload(segment.result_payload) == payload


@pytest.mark.parametrize("kind", ["image", "audio", "resource"])
def test_mcp_embedded_media_preserves_bytes_for_asset_persistence(kind):
    from lumen.services.sandbox_binding import _project_mcp_result

    data = b"\x89PNG\r\n\x1a\n\x00\x00tool-result"
    encoded = base64.b64encode(data).decode()
    part = ({"type": "resource", "resource": {"uri": "file:///work/result.png", "mimeType": "image/png", "blob": encoded}}
            if kind == "resource" else {"type": kind, "mimeType": "image/png", "data": encoded})
    result = _project_mcp_result({"state": "succeeded", "mcp_result": {"content": [part]}})
    assert result.status == "completed"
    assert result.generated_files[0].data == data
    assert result.generated_files[0].media_type == "image/png"
    assert result.generated_files[0].name in result.model_content


@pytest.mark.parametrize("part", [
    {"type": "resource_link", "uri": "https://must-not-fetch.invalid/private"},
    {"type": "image", "mimeType": "image/png", "data": "not-base64"},
])
def test_mcp_unsupported_or_invalid_content_is_not_content_free_success(part):
    from lumen.services.sandbox_binding import _project_mcp_result

    result = _project_mcp_result({"state": "succeeded", "mcp_result": {"content": [part]}})
    assert result.status == "failed"
    assert result.error_code == "sandbox_result_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["image", "manifest", "server", "disabled"])
async def test_pinned_binding_rejects_execution_destination_drift(tmp_path, monkeypatch, manifest, change):
    from lumen.services import sandbox_binding
    from lumen.services.tools import ToolContext

    data = _bytes(manifest)
    runtime = _sandbox_runtime(tmp_path / "tools.json", _pin(data))
    settings = SimpleNamespace(runtime_config=runtime)
    monkeypatch.setattr(sandbox_binding, "get_settings", lambda: settings)
    context = ToolContext(project_id="project", user_id="user", run_id="run", tool_call_id="call")
    definition = parse_tool_manifest(data, _pin(data))[0].tools[0]
    binding = sandbox_binding.kubernetes_mcp_binding(
        context, lease_owner="worker#1", fence=1, profile_id="playwright", tool_def=definition, pool_name="browser",
    )
    revised = runtime.model_dump(mode="json")
    if change == "image":
        revised["pools"][0]["image"] = "registry.example/sandbox@sha256:" + "b" * 64
    elif change == "manifest":
        revised["pools"][0]["profile"]["tool_manifest_sha256"] = "d" * 64
    elif change == "server":
        revised["kubernetes_clusters"][0]["server"] = "https://192.0.2.2:6443"
    else:
        revised["enabled"] = False
    settings.runtime_config = type(runtime).model_validate_json(json.dumps(revised))
    result = await binding.execute({}, context)
    assert result.status == "failed"
    assert result.error_code == "sandbox_configuration_changed"


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "type", "oversized", "remote_ref"])
async def test_dispatch_validates_exact_pinned_schema_without_network(manifest, fault):
    from lumen_plugin_api.tools import ToolBinding, ToolExecutionResult

    from lumen.services.agent_runtime_v2 import dispatch_tool_call

    schema = {"type": "object", "properties": {"nested": {"type": "object"}}}
    if fault == "remote_ref":
        schema = {"$ref": "https://must-not-fetch.invalid/schema"}
    manifest["profiles"][0]["tools"][0]["input_schema"] = schema
    data = _bytes(manifest)
    definition = parse_tool_manifest(data, _pin(data))[0].tools[0]
    calls = []

    async def execute(arguments, _ctx):
        calls.append(arguments)
        return ToolExecutionResult(status="completed", model_content="Executed pinned MCP tool.")

    arguments = {"nested": {"server_specific": True}, "optional": True}
    if fault == "type":
        arguments = {"nested": 123}
    elif fault == "oversized":
        arguments = {"large": "a" * (256 * 1024)}
    result = await dispatch_tool_call(ToolBinding(definition=definition, execute=execute), arguments, object())
    if fault is None:
        assert result.status == "completed"
        assert calls == [arguments]
    else:
        assert result.error_code == "invalid_tool_arguments"
        assert not calls


@pytest.mark.parametrize(("location", "value"), [
    (("version",), True), (("version",), 2), (("profiles",), {}),
    (("profiles", 0, "id"), "Invalid"), (("profiles", 0, "id"), "a" * 65),
    (("profiles", 0, "command"), []), (("profiles", 0, "command"), ["node"]),
    (("profiles", 0, "command"), ["/node", 1]),
    (("profiles", 0, "command"), ["/node\u0000"]),
    (("profiles", 0, "tools"), []), (("profiles", 0, "environment"), {"TOKEN": 1}),
    (("profiles", 0, "tools", 0, "name"), ""),
    (("profiles", 0, "tools", 0, "name"), "a" * 129),
    (("profiles", 0, "tools", 0, "name"), " browser_navigate "),
    (("profiles", 0, "tools", 0, "input_schema"), []),
    (("profiles", 0, "tools", 0, "input_schema"), {"type": "invalid"}),
    (("profiles", 0, "tools", 0, "effect"), "execute"),
    (("profiles", 0, "tools", 0, "description"), 123),
    (("extra",), True), (("profiles", 0, "extra"), True),
    (("profiles", 0, "tools", 0, "extra"), True),
])
def test_loader_rejects_bad_schema(manifest, location, value):
    target = manifest
    for key in location[:-1]:
        target = target[key]
    target[location[-1]] = value
    data = _bytes(manifest)
    with pytest.raises(ToolManifestError, match="^runtime_kubernetes_manifest$"):
        parse_tool_manifest(data, _pin(data))


@pytest.mark.parametrize("duplicate", ["profile", "tool", "cross-profile-tool"])
def test_loader_rejects_duplicate_names(manifest, duplicate):
    profile = manifest["profiles"][0]
    if duplicate == "tool":
        profile["tools"].append(copy.deepcopy(profile["tools"][0]))
    else:
        other = copy.deepcopy(profile)
        if duplicate == "cross-profile-tool":
            other["id"] = "other"
        manifest["profiles"].append(other)
    data = _bytes(manifest)
    with pytest.raises(ToolManifestError):
        parse_tool_manifest(data, _pin(data))


@pytest.mark.parametrize("data", [b"not json", b"\xff", b'{"version":1,"version":1,"profiles":[]}',
                                  b'{"version":NaN,"profiles":[]}', b"[{}]"])
def test_loader_rejects_invalid_json(data):
    with pytest.raises(ToolManifestError):
        parse_tool_manifest(data, _pin(data))


def test_loader_rejects_missing_hash_mismatch_and_oversized(tmp_path, manifest):
    path = tmp_path / "tools.json"
    with pytest.raises(ToolManifestError):
        load_tool_manifest(path, "0" * 64)
    data = _bytes(manifest)
    path.write_bytes(data)
    with pytest.raises(ToolManifestError):
        load_tool_manifest(path, "0" * 64)
    oversized = data + b" " * MAX_TOOL_MANIFEST_BYTES
    path.write_bytes(oversized)
    with pytest.raises(ToolManifestError):
        load_tool_manifest(path, _pin(oversized))


def test_loader_bounds_reads(monkeypatch, manifest):
    data = _bytes(manifest)
    reads = []

    class Source:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size):
            reads.append(size)
            return data

    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: Source())
    load_tool_manifest("/tools.json", _pin(data))
    assert reads == [MAX_TOOL_MANIFEST_BYTES + 1]


@pytest.mark.parametrize("effect", ["read", "workspace_write", "process", "external_mutation"])
def test_loader_accepts_allowed_effects(manifest, effect):
    manifest["profiles"][0]["tools"][0]["effect"] = effect
    data = _bytes(manifest)
    assert parse_tool_manifest(data, _pin(data))[0].tools[0].effect == effect


def test_worker_validates_every_enabled_kubernetes_manifest(tmp_path, manifest):
    data = _bytes(manifest)
    path = tmp_path / "tools.json"
    path.write_bytes(data)
    runtime = SimpleNamespace(enabled=True, pools=[_pool(path, _pin(data)), _pool(path, "0" * 64)])
    with pytest.raises(ToolManifestError):
        validate_worker_tool_manifests(runtime)
    runtime.pools.pop()
    validate_worker_tool_manifests(runtime)


@pytest.mark.parametrize(("runtime_enabled", "pool_enabled", "backend", "role"), [
    (False, True, "kubernetes", "sandbox"), (True, False, "kubernetes", "sandbox"),
    (True, True, "nova", "sandbox"), (True, True, "kubernetes", "worker"),
])
def test_worker_only_requires_enabled_runtime_kubernetes_sandbox_manifests(
    runtime_enabled, pool_enabled, backend, role,
):
    pool = _pool("/does-not-exist", "0" * 64, enabled=pool_enabled, backend=backend, role=role)
    validate_worker_tool_manifests(SimpleNamespace(enabled=runtime_enabled, pools=[pool]))


def test_worker_main_exits_before_registration_on_bad_manifest(monkeypatch):
    from lumen import worker

    settings = SimpleNamespace(runtime_config=SimpleNamespace(
        enabled=True, pools=[_pool("/does-not-exist", "0" * 64)],
    ))
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "configure_logging", lambda *_args: None)
    monkeypatch.setattr(worker, "get_registry", lambda: pytest.fail("must not initialize plugins or claim runs"))
    with pytest.raises(SystemExit, match="^runtime_kubernetes_manifest$"):
        worker.main()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing", "hash", "schema", "bound_name", "second_pool"])
async def test_graph_rejects_untrusted_or_ambiguous_pinned_tools(tmp_path, monkeypatch, manifest, fault):
    from lumen import config
    from lumen.services import graph, sandbox_binding

    if fault == "schema":
        manifest["profiles"][0]["tools"][0]["effect"] = "unsafe"
    data = _bytes(manifest)
    path = tmp_path / "tools.json"
    if fault != "missing":
        path.write_bytes(data)
    pin = "0" * 64 if fault == "hash" else _pin(data)
    runtime = _sandbox_runtime(path, pin)
    pool = runtime.pools[0]
    pools = (pool, pool.model_copy(update={"name": "browser-two"})) if fault == "second_pool" else (pool,)
    runtime = runtime.model_copy(update={"pools": pools})
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(runtime_config=runtime))
    monkeypatch.setattr(sandbox_binding, "get_settings", lambda: SimpleNamespace(runtime_config=runtime))

    async def bindings(_ctx):
        return {"browser_navigate": object()} if fault == "bound_name" else {}


    monkeypatch.setattr(graph.bindings, "v2_tool_bindings", bindings)

    async def collect():
        return [event async for event in graph.stream(
            model="test-model", messages=[], run_id="run", execution_protocol_version=2,
            sandbox_lease=("worker#1", 1),
        )]

    with pytest.raises(ToolManifestError, match="^runtime_kubernetes_manifest$"):
        await collect()
