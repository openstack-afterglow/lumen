"""Bounded, pinned Kubernetes MCP definitions shared by trusted processes.

This module deliberately does not import the separately packaged sandbox daemon.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from jsonschema import Draft202012Validator, SchemaError
from lumen_plugin_api.tools import MAX_TOOL_ARGUMENT_BYTES, MAX_TOOL_SCHEMA_BYTES, ToolDefinition
from pydantic import field_validator
from referencing import Registry
from referencing.exceptions import Unresolvable

if TYPE_CHECKING:
    from lumen.services.infrastructure.config import RuntimeConfig

MAX_TOOL_MANIFEST_BYTES = 1024 * 1024
_PROFILE_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}")


class ToolManifestError(ValueError):
    """Stable, non-secret startup/dispatch failure."""

    def __init__(self) -> None:
        super().__init__("runtime_kubernetes_manifest")


def _validate_pinned_schema_bounds(value: object, depth: int = 0) -> None:
    """Retain core schema resource bounds without rewriting MCP object semantics."""
    if depth > 16:
        raise ValueError("tool schema exceeds the nesting limit")
    if isinstance(value, (dict, list)):
        if len(value) > 100:
            raise ValueError("tool schema has too many properties or values")
        if isinstance(value, dict):
            if any(not isinstance(key, str) or len(key) > 128 for key in value):
                raise ValueError("tool schema has an invalid key")
            value = value.values()
        for child in value:
            _validate_pinned_schema_bounds(child, depth + 1)


class PinnedMCPToolDefinition(ToolDefinition):
    """Pinned server schemas need not follow the builtin/plugin closed-object policy."""

    @field_validator("input_schema")
    @classmethod
    def validate_schema(cls, value: dict) -> dict:
        # Retain the server's exact JSON Schema; adding additionalProperties=False
        # would change the pinned tool's argument contract. The enclosing loader
        # bounds the bytes and authenticates the schema before constructing this.
        if len(str(value).encode("utf-8")) > MAX_TOOL_SCHEMA_BYTES:
            raise ValueError("tool schema exceeds the byte limit")
        _validate_pinned_schema_bounds(value)
        try:
            Draft202012Validator.check_schema(value)
        except SchemaError:
            raise ValueError("invalid pinned MCP schema") from None
        return value

    def validate_arguments(self, arguments: object) -> dict:
        """Validate the pinned schema without fetching remote references or changing it."""
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        if len(json.dumps(arguments, allow_nan=False).encode("utf-8")) > MAX_TOOL_ARGUMENT_BYTES:
            raise ValueError("tool arguments exceed the byte limit")
        try:
            valid = Draft202012Validator(self.input_schema, registry=Registry()).is_valid(arguments)
        except Unresolvable:
            raise ValueError("tool schema reference is unavailable") from None
        if not valid:
            raise ValueError("tool arguments do not match the pinned schema")
        return arguments


@dataclass(frozen=True)
class ToolManifestProfile:
    id: str
    command: tuple[str, ...]
    tools: tuple[ToolDefinition, ...]
    environment: dict[str, str]


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ToolManifestError()
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ToolManifestError()


def parse_tool_manifest(data: bytes, sha256: str) -> tuple[ToolManifestProfile, ...]:
    """Validate exact pinned bytes before projecting provider-safe typed definitions."""
    if (not data or len(data) > MAX_TOOL_MANIFEST_BYTES
            or hashlib.sha256(data).hexdigest() != sha256):
        raise ToolManifestError()
    try:
        manifest = json.loads(data, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        if (not isinstance(manifest, dict) or set(manifest) != {"version", "profiles"}
                or type(manifest["version"]) is not int or manifest["version"] != 1
                or not isinstance(manifest["profiles"], list)):
            raise ToolManifestError()
        profiles = []
        profile_ids: set[str] = set()
        tool_names: set[str] = set()
        for profile in manifest["profiles"]:
            if (not isinstance(profile, dict) or not {"id", "command", "tools"} <= profile.keys()
                    or profile.keys() - {"id", "command", "tools", "environment"}):
                raise ToolManifestError()
            profile_id = profile["id"]
            command = profile["command"]
            tools = profile["tools"]
            environment = profile.get("environment", {})
            if (not isinstance(profile_id, str) or not _PROFILE_ID.fullmatch(profile_id)
                    or profile_id in profile_ids or not isinstance(command, list) or not command
                    or not all(isinstance(arg, str) and arg and "\0" not in arg for arg in command)
                    or not Path(command[0]).is_absolute() or not isinstance(tools, list) or not tools
                    or not isinstance(environment, dict)
                    or not all(isinstance(k, str) and isinstance(v, str) for k, v in environment.items())):
                raise ToolManifestError()
            profile_ids.add(profile_id)
            definitions = []
            for tool in tools:
                if (not isinstance(tool, dict)
                        or set(tool) != {"name", "description", "input_schema", "effect"}):
                    raise ToolManifestError()
                definition = PinnedMCPToolDefinition.model_validate(
                    {**tool, "source": "workspace", "activity_category": f"도구 · {profile_id}"}, strict=True,
                )
                if definition.name != tool["name"] or definition.name in tool_names:
                    raise ToolManifestError()
                tool_names.add(definition.name)
                definitions.append(definition)
            profiles.append(ToolManifestProfile(profile_id, tuple(command), tuple(definitions), environment))
        return tuple(profiles)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ToolManifestError() from None


def load_tool_manifest(path: str | Path, sha256: str) -> tuple[ToolManifestProfile, ...]:
    """Read at most 1 MiB plus a sentinel byte, failing closed on inaccessible files."""
    try:
        with Path(path).open("rb") as source:
            data = source.read(MAX_TOOL_MANIFEST_BYTES + 1)
    except (OSError, ValueError):
        raise ToolManifestError() from None
    return parse_tool_manifest(data, sha256)


def validate_worker_tool_manifests(config: RuntimeConfig) -> None:
    """Run before worker registration/claims, not only when a run first needs tools."""
    if not config.enabled:
        return
    for pool in config.pools:
        if pool.enabled and pool.role == "sandbox" and pool.backend == "kubernetes":
            load_tool_manifest(pool.profile.tool_manifest_file, pool.profile.tool_manifest_sha256)
