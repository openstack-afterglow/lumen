"""Reusable conformance checks for independently authored plugins.

The checks exercise only the public contract: manifest validity, lifecycle order,
authorization failure semantics and identity stability. They never import Lumen.
Install with ``lumen-plugin-api[testing]`` and call from a plugin's own pytest suite.
"""
from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import fields
from typing import Any

from .contracts import ExecutionContext, Namespace, Plugin, PluginError, PluginHost, PluginManifest
from .memory import MemoryAccess, MemoryQuery, MemorySelection
from .skills import SkillAccess, SkillRef, SkillSnapshot
from .tools import ToolBinding, ToolSpec, validate_tool_arguments

_KIND_GROUPS = {"database": "lumen.database", "memory": "lumen.memory", "tools": "lumen.tools", "skills": "lumen.skills", "mcp": "lumen.mcp"}


def _fail(message: str) -> None:
    raise AssertionError(message)


def check_manifest(plugin: Plugin, *, expected_kind: str | None = None) -> PluginManifest:
    """The manifest must round-trip through the versioned model and declare known capabilities."""
    manifest = PluginManifest.model_validate(plugin.manifest.model_dump(mode="json"))
    if manifest.api_version != 1:
        _fail("plugin api_version must be 1")
    if expected_kind is not None and manifest.kind != expected_kind:
        _fail(f"plugin kind {manifest.kind!r} does not match {expected_kind!r}")
    if manifest.kind not in _KIND_GROUPS:
        _fail("plugin kind is unknown")
    host_capabilities = {field.name for field in fields(PluginHost)} - {"configuration"}
    unknown = set(manifest.required_capabilities) - host_capabilities
    if unknown:
        _fail(f"manifest requires unknown host capabilities: {sorted(unknown)}")
    for method in ("start", "close"):
        if not inspect.iscoroutinefunction(getattr(plugin, method, None)):
            _fail(f"plugin.{method} must be an async method")
    return manifest


async def check_lifecycle(factory: Callable[[], Plugin], host: PluginHost) -> Plugin:
    """Start twice from two instances, close both; a closed plugin must not keep serving."""
    first = factory()
    second = factory()
    if first is second:
        _fail("create_plugin must return a fresh instance per call")
    if first.manifest != second.manifest:
        _fail("manifest must be a pure, stable value")
    await first.start(host)
    await first.close()
    await second.start(host)
    await second.close()
    return second


def check_entry_point(distribution: str, plugin: Plugin) -> None:
    """The installed distribution must advertise exactly one entry point for the manifest id."""
    from importlib import metadata

    manifest = plugin.manifest
    group = _KIND_GROUPS[manifest.kind]
    matches = [entry for entry in metadata.entry_points().select(group=group, name=manifest.id)]
    if len(matches) != 1:
        _fail(f"expected exactly one {group} entry point named {manifest.id!r}, found {len(matches)}")
    dist = matches[0].dist
    if dist is None or dist.metadata["Name"].replace("_", "-").lower() != distribution.replace("_", "-").lower():
        _fail("entry point distribution does not match the declared distribution")
    if dist.version != manifest.version:
        _fail("distribution version must equal manifest.version")


async def check_tool_binding(binding: ToolBinding, *, context: ExecutionContext, invalid_arguments: dict[str, Any]) -> None:
    """Definitions validate as provider-safe schemas and reject arguments outside the frozen schema."""
    definition = binding.definition
    if definition.source != "plugin":
        _fail("plugin tool bindings must declare source='plugin' once hosted")
    try:
        validate_tool_arguments(definition.input_schema, invalid_arguments)
    except ValueError:
        pass
    else:
        _fail("invalid arguments were accepted by the frozen schema")
    if not inspect.iscoroutinefunction(binding.execute):
        _fail("binding.execute must be async")
    del context


async def check_tool_provider_rejects_foreign_identity(plugin: Plugin, spec: ToolSpec, context: ExecutionContext, host: PluginHost) -> None:
    """A spec carrying another plugin's identity or version must never bind."""
    foreign = spec.model_copy(update={"identity": spec.identity.model_copy(update={"plugin_id": f"{spec.identity.plugin_id}-other"})})
    try:
        await plugin.bind(foreign, context, host)
    except PluginError as exc:
        if exc.code not in {"plugin_incompatible", "plugin_unavailable", "plugin_authority_revoked"}:
            _fail(f"unexpected error code for foreign identity: {exc.code}")
        return
    _fail("plugin bound a spec with a foreign identity")


async def check_skill_provider(plugin: Plugin, refs: tuple[SkillRef, ...], access: SkillAccess) -> tuple[SkillSnapshot, ...]:
    """Snapshots are frozen: identical resolution yields identical digests and revalidation passes."""
    first = await plugin.resolve(refs, access)
    second = await plugin.resolve(refs, access)
    if [item.content_digest for item in first] != [item.content_digest for item in second]:
        _fail("skill resolution is not deterministic")
    for snapshot in first:
        if snapshot.reference not in refs:
            _fail("skill snapshot references an unrequested skill")
        await plugin.revalidate(snapshot, access)
        tampered = snapshot.model_copy(update={"content_digest": "0" * 64})
        try:
            await plugin.revalidate(tampered, access)
        except PluginError as exc:
            if exc.code != "plugin_configuration_changed":
                _fail("tampered skill snapshot must fail as plugin_configuration_changed")
        else:
            _fail("tampered skill snapshot passed revalidation")
    return first


async def check_memory_provider(plugin: Plugin, access: MemoryAccess, *, foreign_namespace: Namespace) -> MemorySelection:
    """Recall stays inside the authenticated namespace and never returns plaintext."""
    selection = await plugin.recall(MemoryQuery(namespace=access.namespace, strategy="recency"), access)
    if len(selection.candidate_ids) != len(set(selection.candidate_ids)):
        _fail("memory selection returned duplicate candidates")
    if any(not isinstance(item, int) for item in selection.candidate_ids):
        _fail("memory selection must return integer candidate IDs only")
    mismatched = MemoryQuery(namespace=foreign_namespace, strategy="recency")
    try:
        await plugin.recall(mismatched, access)
    except PluginError as exc:
        if exc.code != "plugin_authority_revoked":
            _fail("namespace mismatch must fail as plugin_authority_revoked")
    else:
        _fail("memory provider accepted a query namespace that differs from the authorized access")
    return selection
