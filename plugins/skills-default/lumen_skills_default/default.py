"""Resolve frozen instruction snapshots for integer database refs and explicitly
named plugin-binding refs. This plugin never touches storage directly: every
lookup goes through the host-scoped ``ExtensionAccess`` supplied on ``SkillAccess``,
so the plugin carries no SQL, no credentials, and no lumen-internal imports.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from lumen_plugin_api.contracts import PluginError, PluginExport, PluginHost, PluginIdentity, PluginManifest
from lumen_plugin_api.skills import SkillAccess, SkillRef, SkillRefs, SkillSnapshot

_CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 190},
        "description": {"type": "string", "maxLength": 500},
        "instructions": {"type": "string", "minLength": 1, "maxLength": 100_000},
    },
    "required": ["name", "instructions"],
    "additionalProperties": False,
}


def _digest(material: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False).encode()
    ).hexdigest()


def _require_instruction(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PluginError("plugin_unavailable", "skill has no usable instructions")
    return value


def _snapshot_from_database(ref: SkillRef, item: dict[str, Any], *, plugin_id: str, plugin_version: str) -> SkillSnapshot:
    """Frozen resolution for an integer ``chat_skills`` row (host-authorized)."""
    instruction = _require_instruction(item.get("instructions"))
    material = {
        "id": item.get("id"),
        "scope": item.get("scope"),
        "name": item.get("name"),
        "description": item.get("description"),
        "instructions": instruction,
        "is_active": item.get("is_active"),
    }
    digest = _digest(material)
    return SkillSnapshot(
        reference=ref,
        identity=PluginIdentity(plugin_id=plugin_id, version=plugin_version, config_fingerprint=digest),
        version="1",
        content_digest=digest,
        instruction=instruction,
        name=item.get("name") or "",
        resources=(),
    )


def _snapshot_from_binding(ref: SkillRef, item: dict[str, Any], *, plugin_id: str, plugin_version: str) -> SkillSnapshot:
    """Frozen resolution for an explicitly named ``chat_plugin_bindings`` export."""
    config = item.get("config") if isinstance(item.get("config"), dict) else {}
    instruction = _require_instruction(config.get("instructions"))
    material = {
        "id": item.get("id"),
        "plugin_id": item.get("plugin_id"),
        "export_key": item.get("export_key"),
        "name": item.get("name"),
        "config": config,
        "config_version": item.get("config_version"),
        "is_active": item.get("is_active"),
    }
    digest = _digest(material)
    version = str(item.get("config_version") or 1)
    return SkillSnapshot(
        reference=ref,
        identity=PluginIdentity(plugin_id=plugin_id, version=plugin_version, config_fingerprint=digest),
        version=version,
        content_digest=digest,
        instruction=instruction,
        name=item.get("name") or "",
        resources=(),
    )


class DefaultSkills:
    """Declarative skills provider: instructions only, no execution authority."""

    manifest = PluginManifest(
        id="default-skills",
        kind="skills",
        version="0.1.0",
        required_capabilities=(),
        exports=(
            PluginExport(
                key="default",
                kind="skill",
                name="Custom skill",
                configuration_schema=_CONFIG_SCHEMA,
                user_configurable=True,
            ),
        ),
    )

    async def start(self, host: PluginHost) -> None:
        return None

    async def close(self) -> None:
        return None

    def _snapshot(self, ref: SkillRef, item: dict[str, Any]) -> SkillSnapshot:
        if ref.database_id is not None:
            return _snapshot_from_database(ref, item, plugin_id=self.manifest.id, plugin_version=self.manifest.version)
        return _snapshot_from_binding(ref, item, plugin_id=self.manifest.id, plugin_version=self.manifest.version)

    async def resolve(self, refs: SkillRefs, access: SkillAccess) -> tuple[SkillSnapshot, ...]:
        snapshots = []
        for ref in refs:
            identifier = ref.database_id if ref.database_id is not None else ref.binding_id
            item = await access.extensions.resolve("skill", identifier, access.namespace)
            snapshots.append(self._snapshot(ref, item))
        return tuple(snapshots)

    async def revalidate(self, snapshot: SkillSnapshot, access: SkillAccess) -> None:
        ref = snapshot.reference
        identifier = ref.database_id if ref.database_id is not None else ref.binding_id
        item = await access.extensions.resolve("skill", identifier, access.namespace)
        current = self._snapshot(ref, item)
        if current.content_digest != snapshot.content_digest or current.version != snapshot.version:
            raise PluginError("plugin_configuration_changed", "skill content changed since it was frozen")


def create_plugin() -> DefaultSkills:
    return DefaultSkills()
