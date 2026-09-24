"""Core-owned ``ExtensionAccess`` view scoped exclusively to the ``skill`` kind.

The declarative skills provider never touches storage: it only calls the
``ExtensionAccess`` object supplied on ``SkillAccess``.
This module is that object's real, authorized implementation — it resolves integer
``chat_skills`` refs through the existing encrypted extensions store and
explicitly named plugin-binding refs (UUID) through the existing installed
plugin bindings helper. Both already enforce ownership/scope; this module adds
no new SQL and no new authorization logic of its own.
"""
from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from lumen_plugin_api.contracts import Namespace, PluginError

from lumen.plugins import bindings
from lumen.plugins.registry import fingerprint as _registry_fingerprint
from lumen.services import extensions_store as es

_KIND = "skill"


def _require_project(namespace: Namespace) -> str:
    if not namespace.project_id:
        raise PluginError("plugin_unavailable", "skill resolution requires a project scope")
    return namespace.project_id


class SkillsExtensionAccess:
    """``ExtensionAccess`` narrowed to ``kind == "skill"``; any other kind fails closed."""

    def _guard(self, kind: str) -> None:
        if kind != _KIND:
            raise PluginError("plugin_unavailable", "this host view only serves skill extensions")

    async def list(self, kind: Literal["tool", "skill", "mcp"], namespace: Namespace) -> list[dict[str, Any]]:
        self._guard(kind)
        project_id = _require_project(namespace)
        try:
            db_items = await es.list_for_user(_KIND, user_id=namespace.user_id, project_id=project_id, active_only=True)
        except es.ChatStorageUnavailable as exc:
            raise PluginError("plugin_unavailable", str(exc)) from exc
        binding_items = await bindings.list_bindings(namespace)
        return [*db_items, *[item for item in binding_items if item["kind"] == _KIND]]

    async def resolve(self, kind: Literal["tool", "skill", "mcp"], identifier: int | str, namespace: Namespace) -> dict[str, Any]:
        self._guard(kind)
        if isinstance(identifier, bool):
            raise PluginError("plugin_unavailable", "invalid skill reference")
        if isinstance(identifier, int):
            if identifier < 1:
                raise PluginError("plugin_unavailable", "invalid skill reference")
            project_id = _require_project(namespace)
            try:
                items = await es.list_for_user(_KIND, user_id=namespace.user_id, project_id=project_id, active_only=True)
            except es.ChatStorageUnavailable as exc:
                raise PluginError("plugin_unavailable", str(exc)) from exc
            item = next((entry for entry in items if entry.get("id") == identifier), None)
            if item is None or not isinstance(item.get("instructions"), str):
                raise PluginError("plugin_authority_revoked", "skill is unavailable")
            return item
        if isinstance(identifier, str):
            try:
                if str(UUID(identifier)) != identifier:
                    raise ValueError("noncanonical skill binding UUID")
            except ValueError as exc:
                raise PluginError("plugin_unavailable", "invalid skill binding reference") from exc
            _require_project(namespace)
            try:
                return await bindings.resolve_binding(identifier, kind=_KIND, namespace=namespace)
            except es.ChatStorageUnavailable as exc:
                raise PluginError("plugin_unavailable", str(exc)) from exc
        raise PluginError("plugin_unavailable", "invalid skill reference")

    async def revalidate(self, kind: Literal["tool", "skill", "mcp"], identifier: int | str, fingerprint: str, namespace: Namespace) -> dict[str, Any]:
        self._guard(kind)
        item = await self.resolve(kind, identifier, namespace)
        if _registry_fingerprint(item) != fingerprint:
            raise PluginError("plugin_configuration_changed", "skill configuration changed")
        return item


def make_host() -> SkillsExtensionAccess:
    """Factory for the skills-scoped ``ExtensionAccess`` handed to the plugin via ``SkillAccess``."""
    return SkillsExtensionAccess()

async def resolve_skills(
    database_ids: list[int], plugin_snapshots: list[dict], *, user_id: str, project_id: str
) -> tuple[list[str], list[dict]]:
    """Resolve one immutable instruction snapshot for each selected DB or binding ref."""
    from lumen_plugin_api.skills import SkillAccess, SkillRef

    from .registry import get_plugin

    access = SkillAccess(namespace=Namespace(user_id=user_id, project_id=project_id), extensions=make_host())
    instructions: list[str] = []
    frozen: list[dict] = []
    if database_ids:
        provider = get_plugin("skills", "default-skills")
        for snapshot in await provider.resolve(tuple(SkillRef(database_id=identifier) for identifier in database_ids), access):
            instructions.append(snapshot.instruction)
            frozen.append({"snapshot": snapshot.model_dump(mode="json"), "provider_id": provider.manifest.id})
    for binding_snapshot in plugin_snapshots:
        item = binding_snapshot["binding"]
        selected = get_plugin("skills", item["plugin_id"])
        reference = SkillRef(binding_id=item["id"])
        await bindings.revalidate(binding_snapshot, namespace=access.namespace)
        resolved = await selected.resolve((reference,), access)
        if len(resolved) != 1 or resolved[0].reference != reference or resolved[0].identity.plugin_id != item["plugin_id"]:
            raise PluginError("plugin_incompatible", "skill provider returned a mismatched snapshot")
        instructions.append(resolved[0].instruction)
        frozen.append({"snapshot": resolved[0].model_dump(mode="json"), "provider_id": item["plugin_id"], "binding": binding_snapshot})
    return instructions, frozen


async def revalidate_skills(snapshots: list[dict], *, user_id: str, project_id: str) -> None:
    """Reject a changed instruction or revoked binding before each model turn."""
    from lumen_plugin_api.skills import SkillAccess, SkillSnapshot

    from .registry import get_plugin

    namespace = Namespace(user_id=user_id, project_id=project_id)
    access = SkillAccess(namespace=namespace, extensions=make_host())
    for item in snapshots:
        snapshot = SkillSnapshot.model_validate(item["snapshot"])
        if item.get("provider_id") != snapshot.identity.plugin_id:
            raise PluginError("plugin_incompatible", "skill provider identity changed")
        binding_snapshot = item.get("binding")
        if binding_snapshot is not None:
            if snapshot.reference.binding_id != binding_snapshot["binding"]["id"]:
                raise PluginError("plugin_incompatible", "skill reference changed")
            await bindings.revalidate(binding_snapshot, namespace=namespace)
        elif snapshot.reference.database_id is None:
            raise PluginError("plugin_incompatible", "skill reference is invalid")
        provider = get_plugin("skills", item["provider_id"])
        await provider.revalidate(snapshot, access)
