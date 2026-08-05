"""Declarative extension-package manifest validation for the v2 executor.

Packages are data only.  This boundary rejects executable hooks and credential
material before a manifest can be encrypted or materialized into project rows.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_NAME = re.compile(r"^[a-z][a-z0-9-]{0,99}$")
_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$")
_COMPONENTS = frozenset({"skills", "commands", "agents", "custom_tools", "mcp_servers"})
_FORBIDDEN_KEYS = frozenset(
    {"token", "password", "secret", "api_key", "authorization", "hook", "script", "command", "stdio"}
)


class ExtensionPackageValidationError(ValueError):
    pass


def _forbid_executable_or_secret_values(value: object, *, path: str = "manifest") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ExtensionPackageValidationError(f"{path} keys must be strings")
            normalized = key.lower().replace("-", "_")
            if normalized in _FORBIDDEN_KEYS or normalized.endswith("_token") or normalized.endswith("_secret"):
                raise ExtensionPackageValidationError(f"{path}.{key} is not allowed in a declarative package")
            _forbid_executable_or_secret_values(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _forbid_executable_or_secret_values(child, path=f"{path}[{index}]")
    elif isinstance(value, str) and value.lower().startswith(("stdio:", "file:", "unix:")):
        raise ExtensionPackageValidationError(f"{path} must not use a local transport")


def validate_manifest(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExtensionPackageValidationError("package manifest must be an object")
    expected = {"schema_version", "name", "version", "description", "components"}
    if set(value) != expected:
        raise ExtensionPackageValidationError("package manifest contains unknown or missing fields")
    if value["schema_version"] != 1:
        raise ExtensionPackageValidationError("package manifest schema_version must be 1")
    name = value["name"]
    version = value["version"]
    description = value["description"]
    components = value["components"]
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ExtensionPackageValidationError("package name must be a slug")
    if not isinstance(version, str) or not _SEMVER.fullmatch(version):
        raise ExtensionPackageValidationError("package version must be semantic version")
    if not isinstance(description, str) or not description.strip() or len(description) > 1_000:
        raise ExtensionPackageValidationError("package description is invalid")
    if not isinstance(components, dict) or set(components) - _COMPONENTS:
        raise ExtensionPackageValidationError("package components are invalid")
    normalized: dict[str, list[dict[str, Any]]] = {}
    refs: set[tuple[str, str]] = set()
    for component_type in _COMPONENTS:
        entries = components.get(component_type, [])
        if not isinstance(entries, list) or len(entries) > 100:
            raise ExtensionPackageValidationError(f"package {component_type} must be a bounded list")
        parsed: list[dict[str, Any]] = []
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("ref"), str)
                or not _NAME.fullmatch(entry["ref"])
            ):
                raise ExtensionPackageValidationError(f"package {component_type} entry requires a slug ref")
            identity = (component_type, entry["ref"])
            if identity in refs:
                raise ExtensionPackageValidationError("package component refs must be unique")
            refs.add(identity)
            _forbid_executable_or_secret_values(entry, path=f"components.{component_type}.{entry['ref']}")
            parsed.append(dict(entry))
        normalized[component_type] = parsed
    return {
        "schema_version": 1,
        "name": name,
        "version": version,
        "description": description.strip(),
        "components": normalized,
    }


def manifest_hash(manifest: dict[str, Any]) -> str:
    validated = validate_manifest(manifest)
    return hashlib.sha256(json.dumps(validated, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
