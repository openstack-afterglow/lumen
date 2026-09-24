"""Independent behavioral conformance tests for the Afterglow MCP authority plugin.

These tests exercise ``AfterglowMcp`` purely through the public ``McpProvider``
surface (describe/bind/revalidate) against a fake ``McpAuthorityAccess``; no
Lumen core module is imported.
"""
from __future__ import annotations

from typing import Any

import pytest
from lumen_mcp_default.afterglow import AfterglowMcp, create_afterglow_plugin
from lumen_plugin_api.contracts import ExecutionContext, Namespace, PluginError, PluginHost
from lumen_plugin_api.mcp import McpSelection, McpSnapshot
from lumen_plugin_api.testing import check_entry_point, check_lifecycle, check_manifest

USER = "user-1"
PROJECT = "project-1"


def _delegated(*, grant_id="grant-1", user_id=USER, project_id=PROJECT, credential_epoch=1, selection_generation=0):
    return {
        "grant_id": grant_id,
        "user_id": user_id,
        "project_id": project_id,
        "credential_epoch": credential_epoch,
        "selection_generation": selection_generation,
    }


READ_ENTRY = {"name": "search_notes", "description": "Search notes", "input_schema": {"type": "object", "properties": {}}, "effect": "read"}
MUTATE_ENTRY = {"name": "create_task", "description": "Create a task", "input_schema": {"type": "object", "properties": {}}, "effect": "external_mutation"}
FINGERPRINT = "a" * 64


class FakeAuthority:
    """In-memory double for the host-owned ``McpAuthorityAccess`` bridge transport."""

    def __init__(self, *, snapshot=None, entries=(), fingerprint=FINGERPRINT):
        self.snapshot = snapshot
        self.entries = list(entries)
        self.fingerprint = fingerprint
        self.registry_calls = 0
        self.read_calls: list[tuple[Any, ...]] = []
        self.preview_calls: list[tuple[Any, ...]] = []
        self.claim_calls: list[tuple[Any, ...]] = []
        self.fail_registry: PluginError | None = None

    async def registry(self, namespace: Namespace) -> dict[str, Any]:
        self.registry_calls += 1
        if self.fail_registry is not None:
            raise self.fail_registry
        return {"snapshot": self.snapshot, "entries": self.entries, "service_fingerprint": self.fingerprint}

    async def read(self, snapshot, name, arguments) -> dict[str, Any]:
        self.read_calls.append((snapshot, name, arguments))
        return {"ok": True, "tool": name}

    async def preview(self, snapshot, name, arguments) -> dict[str, Any]:
        self.preview_calls.append((snapshot, name, arguments))
        return {"intended_transition": "create", "resource_identity": name}

    async def claim(self, snapshot, name, arguments, idempotency_key) -> dict[str, Any]:
        self.claim_calls.append((snapshot, name, arguments, idempotency_key))
        return {"ok": True, "tool": name, "idempotency_key": idempotency_key}

    async def complete(self, invocation_id) -> dict[str, Any]:
        return {}


def _host(authority: FakeAuthority | None) -> PluginHost:
    return PluginHost(configuration={}, mcp_authority=authority)


def _selection(*, delegated_snapshot=None) -> McpSelection:
    return McpSelection(namespace=Namespace(user_id=USER, project_id=PROJECT), authority="afterglow", delegated_snapshot=delegated_snapshot)


def _context(*, run_id="run-1", call_id="call-1") -> ExecutionContext:
    return ExecutionContext(user_id=USER, project_id=PROJECT, run_id=run_id, call_id=call_id)

async def _plugin(authority: FakeAuthority | None) -> AfterglowMcp:
    plugin = create_afterglow_plugin()
    await plugin.start(_host(authority))
    return plugin

async def test_entry_point_manifest_and_lifecycle() -> None:
    plugin = create_afterglow_plugin()
    assert check_manifest(plugin, expected_kind="mcp").id == "afterglow-mcp"
    check_entry_point("lumen-mcp-default", plugin)
    closed = await check_lifecycle(create_afterglow_plugin, _host(FakeAuthority(snapshot=_delegated(), entries=[READ_ENTRY])))
    with pytest.raises(PluginError) as excinfo:
        await closed.describe(_selection())
    assert excinfo.value.code == "plugin_unavailable"


async def test_start_requires_mcp_authority_capability() -> None:
    with pytest.raises(PluginError) as excinfo:
        await _plugin(None)
    assert excinfo.value.code == "plugin_unavailable"


async def test_describe_returns_empty_when_no_grant_selected() -> None:
    plugin = await _plugin(FakeAuthority(snapshot=None))
    assert await plugin.describe(_selection()) == ()


async def test_describe_builds_snapshot_preserving_epoch_generation_and_fingerprint() -> None:
    delegated = _delegated(credential_epoch=3, selection_generation=7)
    authority = FakeAuthority(snapshot=delegated, entries=[READ_ENTRY, MUTATE_ENTRY], fingerprint=FINGERPRINT)
    plugin = await _plugin(authority)

    snapshots = await plugin.describe(_selection())

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.authority == "afterglow"
    assert snapshot.grant_id == delegated["grant_id"]
    assert snapshot.credential_epoch == 3
    assert snapshot.selection_generation == 7
    assert snapshot.fingerprint == FINGERPRINT
    assert snapshot.identity.config_fingerprint == FINGERPRINT
    assert {tool.name for tool in snapshot.tools} == {"search_notes", "create_task"}
    read_def = next(tool for tool in snapshot.tools if tool.name == "search_notes")
    mutate_def = next(tool for tool in snapshot.tools if tool.name == "create_task")
    assert read_def.effect == "read" and read_def.parallel_safe is True
    assert mutate_def.effect == "external_mutation" and mutate_def.parallel_safe is False


async def test_describe_returns_empty_on_namespace_ownership_mismatch() -> None:
    delegated = _delegated(user_id="someone-else")
    authority = FakeAuthority(snapshot=delegated, entries=[READ_ENTRY])
    plugin = await _plugin(authority)
    assert await plugin.describe(_selection()) == ()


async def test_describe_raises_when_delegated_selection_changed() -> None:
    stale = _delegated(grant_id="grant-old")
    live = _delegated(grant_id="grant-new")
    authority = FakeAuthority(snapshot=live, entries=[READ_ENTRY])
    plugin = await _plugin(authority)

    with pytest.raises(PluginError) as excinfo:
        await plugin.describe(_selection(delegated_snapshot=stale))
    assert excinfo.value.code == "plugin_authority_revoked"


async def test_describe_fails_closed_on_malformed_entry_effect() -> None:
    bad_entry = {**READ_ENTRY, "effect": "delete_everything"}
    authority = FakeAuthority(snapshot=_delegated(), entries=[bad_entry])
    plugin = await _plugin(authority)

    with pytest.raises(PluginError) as excinfo:
        await plugin.describe(_selection())
    assert excinfo.value.code == "plugin_configuration_changed"


async def test_describe_propagates_authority_unavailable() -> None:
    authority = FakeAuthority()
    authority.fail_registry = PluginError("plugin_unavailable", "bridge down")
    plugin = await _plugin(authority)

    with pytest.raises(PluginError) as excinfo:
        await plugin.describe(_selection())
    assert excinfo.value.code == "plugin_unavailable"


async def _described(authority: FakeAuthority) -> McpSnapshot:
    plugin = await _plugin(authority)
    return (await plugin.describe(_selection()))[0]


async def test_revalidate_passes_when_authority_unchanged() -> None:
    authority = FakeAuthority(snapshot=_delegated(), entries=[READ_ENTRY])
    plugin = await _plugin(authority)
    snapshot = await _described(authority)
    await plugin.revalidate(snapshot)  # must not raise


async def test_revalidate_fails_closed_on_epoch_change() -> None:
    authority = FakeAuthority(snapshot=_delegated(credential_epoch=1), entries=[READ_ENTRY])
    plugin = await _plugin(authority)
    snapshot = await _described(authority)

    authority.snapshot = _delegated(credential_epoch=2)
    with pytest.raises(PluginError) as excinfo:
        await plugin.revalidate(snapshot)
    assert excinfo.value.code == "plugin_configuration_changed"


async def test_revalidate_fails_closed_on_fingerprint_change() -> None:
    authority = FakeAuthority(snapshot=_delegated(), entries=[READ_ENTRY], fingerprint=FINGERPRINT)
    plugin = await _plugin(authority)
    snapshot = await _described(authority)

    authority.fingerprint = "b" * 64
    with pytest.raises(PluginError) as excinfo:
        await plugin.revalidate(snapshot)
    assert excinfo.value.code == "plugin_configuration_changed"


async def test_bind_rejects_context_namespace_mismatch() -> None:
    authority = FakeAuthority(snapshot=_delegated(), entries=[READ_ENTRY])
    plugin = await _plugin(authority)
    snapshot = await _described(authority)

    other_context = ExecutionContext(user_id="other-user", project_id=PROJECT)
    with pytest.raises(PluginError) as excinfo:
        await plugin.bind(snapshot, other_context)
    assert excinfo.value.code == "plugin_authority_revoked"


async def test_bind_read_tool_dispatches_via_authority_read() -> None:
    authority = FakeAuthority(snapshot=_delegated(), entries=[READ_ENTRY])
    plugin = await _plugin(authority)
    snapshot = await _described(authority)

    bindings = await plugin.bind(snapshot, _context())
    assert len(bindings) == 1
    binding = bindings[0]
    assert binding.preview is None

    result = await binding.execute({"query": "hi"}, _context())
    assert result.status == "completed"
    assert authority.read_calls == [({"grant_id": "grant-1", "user_id": USER, "project_id": PROJECT, "credential_epoch": 1, "selection_generation": 0}, "search_notes", {"query": "hi"})]
    assert authority.claim_calls == []


async def test_bind_mutation_tool_exposes_preview_and_reuses_idempotency_key() -> None:
    authority = FakeAuthority(snapshot=_delegated(), entries=[MUTATE_ENTRY])
    plugin = await _plugin(authority)
    snapshot = await _described(authority)

    binding = (await plugin.bind(snapshot, _context()))[0]
    assert binding.preview is not None

    plan = await binding.preview({"title": "t"}, _context())
    assert plan == {"intended_transition": "create", "resource_identity": "create_task"}
    assert len(authority.preview_calls) == 1

    ctx = _context(run_id="run-1", call_id="call-7")
    await binding.execute({"title": "t"}, ctx)
    await binding.execute({"title": "t"}, ctx)
    assert len(authority.claim_calls) == 2
    first_key = authority.claim_calls[0][3]
    second_key = authority.claim_calls[1][3]
    assert first_key == second_key == "run-1:call-7"

    other_ctx = _context(run_id="run-1", call_id="call-8")
    await binding.execute({"title": "t"}, other_ctx)
    assert authority.claim_calls[2][3] != first_key


async def test_bind_mutation_requires_durable_call_identity() -> None:
    authority = FakeAuthority(snapshot=_delegated(), entries=[MUTATE_ENTRY])
    plugin = await _plugin(authority)
    snapshot = await _described(authority)
    binding = (await plugin.bind(snapshot, _context()))[0]

    ephemeral_ctx = ExecutionContext(user_id=USER, project_id=PROJECT, run_id=None, call_id=None)
    with pytest.raises(PluginError) as excinfo:
        await binding.execute({"title": "t"}, ephemeral_ctx)
    assert excinfo.value.code == "plugin_configuration_changed"


async def test_bind_execute_revalidates_and_fails_closed_when_authority_changes() -> None:
    authority = FakeAuthority(snapshot=_delegated(credential_epoch=1), entries=[READ_ENTRY])
    plugin = await _plugin(authority)
    snapshot = await _described(authority)
    binding = (await plugin.bind(snapshot, _context()))[0]

    authority.snapshot = _delegated(credential_epoch=2)
    with pytest.raises(PluginError) as excinfo:
        await binding.execute({"query": "hi"}, _context())
    assert excinfo.value.code == "plugin_configuration_changed"
    assert authority.read_calls == []
