"""Delegated Afterglow MCP authority.

Only an opaque delegated-grant snapshot ever crosses into this plugin: no
OAuth token, connection store, or user Keystone credential is reachable here.
All authority validation (owner/grant/epoch/generation/fingerprint equality,
registry entry parsing, effect-based read/mutation dispatch, and mutation
idempotency) lives in this module; ``host.mcp_authority`` is a thin transport
that only knows how to reach the configured Afterglow bridge.
"""
from __future__ import annotations

import json
from typing import Any

from lumen_plugin_api.contracts import (
    ExecutionContext,
    Namespace,
    PluginError,
    PluginHost,
    PluginIdentity,
    PluginManifest,
)
from lumen_plugin_api.mcp import McpSelection, McpSnapshot
from lumen_plugin_api.tools import ToolBinding, ToolDefinition, ToolExecutionResult
from pydantic import ValidationError

_EFFECTS = {"read", "external_mutation"}
_SNAPSHOT_KEYS = {"grant_id", "user_id", "project_id", "credential_epoch", "selection_generation"}


def _delegated(value: object) -> dict[str, Any] | None:
    """Parse and validate the opaque delegated snapshot; never widen its shape."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _SNAPSHOT_KEYS:
        raise PluginError("plugin_configuration_changed", "Afterglow delegated MCP snapshot is invalid")
    grant_id, user_id, project_id = value["grant_id"], value["user_id"], value["project_id"]
    credential_epoch, selection_generation = value["credential_epoch"], value["selection_generation"]
    if (
        not all(isinstance(item, str) and item for item in (grant_id, user_id, project_id))
        or type(credential_epoch) is not int
        or credential_epoch < 1
        or type(selection_generation) is not int
        or selection_generation < 0
    ):
        raise PluginError("plugin_configuration_changed", "Afterglow delegated MCP snapshot is invalid")
    return value


def _entry(value: object) -> ToolDefinition:
    if not isinstance(value, dict) or set(value) != {"name", "description", "input_schema", "effect"}:
        raise PluginError("plugin_configuration_changed", "Afterglow MCP registry response is invalid")
    name, description, schema, effect = value["name"], value["description"], value["input_schema"], value["effect"]
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 128
        or not isinstance(description, str)
        or not isinstance(schema, dict)
        or effect not in _EFFECTS
    ):
        raise PluginError("plugin_configuration_changed", "Afterglow MCP registry response is invalid")
    try:
        return ToolDefinition(
            name=name,
            description=description,
            input_schema={**schema, "additionalProperties": False},
            effect=effect,
            parallel_safe=effect == "read",
            source="mcp",
        )
    except ValidationError as exc:
        raise PluginError("plugin_configuration_changed", "Afterglow MCP registry response is invalid") from exc


def _parse_registry(raw: object) -> tuple[dict[str, Any] | None, tuple[ToolDefinition, ...], str]:
    if not isinstance(raw, dict):
        raise PluginError("plugin_unavailable", "Afterglow MCP registry response is invalid")
    delegated = _delegated(raw.get("snapshot"))
    if delegated is None:
        return None, (), ""
    entries = raw.get("entries")
    fingerprint = raw.get("service_fingerprint")
    if not isinstance(entries, list) or not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise PluginError("plugin_configuration_changed", "Afterglow MCP registry response is invalid")
    return delegated, tuple(_entry(item) for item in entries), fingerprint


class AfterglowMcp:
    """Delegated Afterglow MCP authority: describe/bind/revalidate over ``McpAuthorityAccess``."""

    manifest = PluginManifest(id="afterglow-mcp", kind="mcp", version="0.1.0", required_capabilities=("mcp_authority",))

    def __init__(self) -> None:
        self._authority = None

    async def start(self, host: PluginHost) -> None:
        if host.mcp_authority is None:
            raise PluginError("plugin_unavailable", "Delegated MCP authority capability is required")
        self._authority = host.mcp_authority

    async def close(self) -> None:
        self._authority = None

    async def _fetch(self, namespace: Namespace) -> tuple[dict[str, Any] | None, tuple[ToolDefinition, ...], str]:
        if self._authority is None:
            raise PluginError("plugin_unavailable", "Delegated MCP plugin has not started")
        raw = await self._authority.registry(namespace)
        return _parse_registry(raw)

    async def describe(self, selection: McpSelection) -> tuple[McpSnapshot, ...]:
        if selection.authority != "afterglow":
            raise PluginError("plugin_authority_revoked")
        namespace = selection.namespace
        if not namespace.project_id:
            return ()
        delegated, tools, fingerprint = await self._fetch(namespace)
        if delegated is None:
            return ()
        if delegated["user_id"] != namespace.user_id or delegated["project_id"] != namespace.project_id:
            return ()
        if selection.delegated_snapshot is not None and _delegated(selection.delegated_snapshot) != delegated:
            raise PluginError("plugin_authority_revoked", "Delegated Afterglow MCP selection changed")
        try:
            identity = PluginIdentity(plugin_id=self.manifest.id, version=self.manifest.version, config_fingerprint=fingerprint)
            snapshot = McpSnapshot(
                authority="afterglow",
                namespace=namespace,
                identity=identity,
                credential_epoch=delegated["credential_epoch"],
                selection_generation=delegated["selection_generation"],
                grant_id=delegated["grant_id"],
                fingerprint=fingerprint,
                tools=tools,
            )
        except ValidationError as exc:
            raise PluginError("plugin_configuration_changed", "Afterglow MCP registry response is invalid") from exc
        return (snapshot,)

    async def revalidate(self, snapshot: McpSnapshot) -> None:
        if snapshot.authority != "afterglow" or snapshot.identity.plugin_id != self.manifest.id:
            raise PluginError("plugin_authority_revoked")
        delegated, _tools, fingerprint = await self._fetch(snapshot.namespace)
        if (
            delegated is None
            or delegated["user_id"] != snapshot.namespace.user_id
            or delegated["project_id"] != snapshot.namespace.project_id
            or delegated["grant_id"] != snapshot.grant_id
            or delegated["credential_epoch"] != snapshot.credential_epoch
            or delegated["selection_generation"] != snapshot.selection_generation
            or fingerprint != snapshot.fingerprint
        ):
            raise PluginError("plugin_configuration_changed", "Delegated Afterglow MCP selection changed")

    async def bind(self, snapshot: McpSnapshot, context: ExecutionContext) -> tuple[ToolBinding, ...]:
        if snapshot.authority != "afterglow" or (context.user_id, context.project_id) != (
            snapshot.namespace.user_id,
            snapshot.namespace.project_id,
        ):
            raise PluginError("plugin_authority_revoked")
        await self.revalidate(snapshot)
        delegated = {
            "grant_id": snapshot.grant_id,
            "user_id": snapshot.namespace.user_id,
            "project_id": snapshot.namespace.project_id,
            "credential_epoch": snapshot.credential_epoch,
            "selection_generation": snapshot.selection_generation,
        }
        bindings: list[ToolBinding] = []
        for definition in snapshot.tools:

            async def execute(arguments: dict[str, Any], ctx: ExecutionContext, *, definition=definition) -> ToolExecutionResult:
                if (ctx.user_id, ctx.project_id) != (snapshot.namespace.user_id, snapshot.namespace.project_id):
                    raise PluginError("plugin_authority_revoked")
                await self.revalidate(snapshot)
                if definition.effect == "read":
                    result = await self._authority.read(delegated, definition.name, arguments)
                else:
                    if not ctx.run_id or not ctx.call_id:
                        raise PluginError("plugin_configuration_changed", "Afterglow mutation requires a durable tool call")
                    result = await self._authority.claim(delegated, definition.name, arguments, f"{ctx.run_id}:{ctx.call_id}")
                if not isinstance(result, dict):
                    raise PluginError("plugin_unavailable", "Afterglow MCP tool result is invalid")
                content = json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                return ToolExecutionResult(status="completed", model_content=content)

            preview = None
            if definition.effect == "external_mutation":

                async def preview_fn(arguments: dict[str, Any], ctx: ExecutionContext, *, definition=definition) -> dict[str, Any]:
                    if (ctx.user_id, ctx.project_id) != (snapshot.namespace.user_id, snapshot.namespace.project_id):
                        raise PluginError("plugin_authority_revoked")
                    await self.revalidate(snapshot)
                    plan = await self._authority.preview(delegated, definition.name, arguments)
                    if not isinstance(plan, dict):
                        raise PluginError("plugin_unavailable", "Afterglow mutation preview is invalid")
                    return plan

                preview = preview_fn

            bindings.append(
                ToolBinding(definition=definition, execute=execute, preview=preview, config_fingerprint=snapshot.fingerprint)
            )
        return tuple(bindings)


def create_afterglow_plugin() -> AfterglowMcp:
    return AfterglowMcp()
