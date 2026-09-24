"""Encrypted, owner-scoped installed exports and immutable execution snapshots."""
from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

from lumen_plugin_api.contracts import ExecutionContext, Namespace, PluginError, PluginIdentity
from lumen_plugin_api.tools import ToolBinding, ToolSpec
from sqlalchemy import and_, or_, select

from lumen.crypto import decrypt_chat_content, encrypt_chat_content
from lumen.db import get_session_factory, is_db_available
from lumen.models.chat_db import ChatPluginBinding
from lumen.services.extensions_store import (
    ChatStorageUnavailable,
    ExtensionForbidden,
    ExtensionNotFound,
    ExtensionValidationError,
)

from .registry import fingerprint, get_plugin, get_registry, validate_configuration


def validate_binding_ids(values: list[str]) -> list[str]:
    if len(values) > 100 or len(set(values)) != len(values):
        raise ValueError("plugin binding IDs must be unique and bounded")
    if any(str(UUID(value)) != value for value in values):
        raise ValueError("plugin binding IDs must be canonical UUID strings")
    return values


def _factory():
    factory = get_session_factory()
    if factory is None or not is_db_available():
        raise ChatStorageUnavailable("chat storage is unavailable")
    return factory


def _visible(namespace: Namespace):
    return or_(ChatPluginBinding.scope == "global", and_(ChatPluginBinding.scope == "user", ChatPluginBinding.owner_user_id == namespace.user_id, ChatPluginBinding.owner_project_id == namespace.project_id))


def _export(kind: str, plugin_id: str, key: str):
    if kind not in {"tool", "skill"}:
        raise ExtensionValidationError("invalid plugin binding kind")
    provider = get_plugin({"tool": "tools", "skill": "skills"}[kind], plugin_id)
    export = next((item for item in provider.manifest.exports if item.kind == kind and item.key == key), None)
    if export is None:
        raise ExtensionValidationError("plugin export is not installed")
    return provider, export


def _safe_config(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if (re.search(r"(?:^|_)(?:password|secret|token|api_key|authorization|credential)$", key, re.I)
                    and (not isinstance(child, dict) or set(child) != {"reference"}
                         or not isinstance(child["reference"], str))):
                raise ExtensionValidationError("credentials require server-side secret references")
            _safe_config(child)
    elif isinstance(value, list):
        for child in value:
            _safe_config(child)


def _validate_config(export, config: dict) -> None:
    _safe_config(config)
    try:
        if len(json.dumps(config).encode()) > 32768:
            raise ValueError("oversized plugin configuration")
        validate_configuration(export.configuration_schema, config)
    except Exception as exc:
        raise ExtensionValidationError("plugin configuration is invalid") from exc


def _public(row: ChatPluginBinding) -> dict[str, Any]:
    return {"id": row.id, "kind": row.kind, "plugin_id": row.plugin_id, "export_key": row.export_key, "name": row.name, "scope": row.scope, "owner_user_id": row.owner_user_id, "owner_project_id": row.owner_project_id, "config": json.loads(decrypt_chat_content(row.encrypted_config)), "config_version": row.config_version, "is_active": row.is_active}


async def list_bindings(namespace: Namespace | None, *, admin: bool = False) -> list[dict]:
    async with _factory()() as session:
        query = select(ChatPluginBinding)
        if admin:
            query = query.where(ChatPluginBinding.scope == "global")
        elif namespace is not None:
            query = query.where(_visible(namespace), ChatPluginBinding.is_active.is_(True))
        else:
            raise ExtensionForbidden("plugin catalogue requires an owner")
        return [_public(row) for row in (await session.execute(query.order_by(ChatPluginBinding.id))).scalars()]


async def resolve_binding(identifier: str, *, kind: str, namespace: Namespace) -> dict:
    async with _factory()() as session:
        row = (await session.execute(select(ChatPluginBinding).where(ChatPluginBinding.id == identifier, ChatPluginBinding.kind == kind, _visible(namespace), ChatPluginBinding.is_active.is_(True)))).scalar_one_or_none()
        if row is None:
            raise PluginError("plugin_authority_revoked")
        return _public(row)


async def create_binding(*, kind: str, plugin_id: str, export_key: str, name: str, config: dict, is_active: bool, namespace: Namespace | None, admin: bool) -> dict:
    _, export = _export(kind, plugin_id, export_key)
    if not admin and (namespace is None or not namespace.project_id or not export.user_configurable):
        raise ExtensionForbidden("plugin export cannot be configured by this owner")
    _validate_config(export, config)
    async with _factory()() as session, session.begin():
        row = ChatPluginBinding(id=str(uuid4()), kind=kind, plugin_id=plugin_id, export_key=export_key, name=name, scope="global" if admin else "user", owner_user_id=None if admin else namespace.user_id, owner_project_id=None if admin else namespace.project_id, encrypted_config=encrypt_chat_content(json.dumps(config)), is_active=is_active, config_version=1)
        session.add(row)
        await session.flush()
        return _public(row)


async def update_binding(identifier: str, *, patch: dict, namespace: Namespace | None, admin: bool, delete: bool = False) -> dict | None:
    if set(patch) - {"name", "config", "is_active"}:
        raise ExtensionValidationError("plugin binding identity is immutable")
    async with _factory()() as session, session.begin():
        row = (await session.execute(select(ChatPluginBinding).where(ChatPluginBinding.id == identifier).with_for_update())).scalar_one_or_none()
        if row is None:
            raise ExtensionNotFound("plugin binding not found")
        if admin:
            permitted = row.scope == "global"
        else:
            permitted = namespace is not None and row.scope == "user" and row.owner_user_id == namespace.user_id and row.owner_project_id == namespace.project_id
        if not permitted:
            raise ExtensionForbidden("plugin binding is not owned by this principal")
        if delete:
            await session.delete(row)
            return None
        _, export = _export(row.kind, row.plugin_id, row.export_key)
        if not admin and not export.user_configurable:
            raise ExtensionForbidden("plugin export is no longer owner configurable")
        if "config" in patch:
            _validate_config(export, patch["config"])
            row.encrypted_config = encrypt_chat_content(json.dumps(patch["config"]))
        for name in ("name", "is_active"):
            if name in patch:
                setattr(row, name, patch[name])
        row.config_version += 1
        await session.flush()
        return _public(row)


def binding_identity(item: dict) -> PluginIdentity:
    provider, export = _export(item["kind"], item["plugin_id"], item["export_key"])
    manifest = provider.manifest
    material = {"binding": item, "export": export.model_dump(mode="json"), "operator_config": get_registry().config.settings.get(manifest.id, {})}
    return PluginIdentity(plugin_id=manifest.id, version=manifest.version, config_fingerprint=fingerprint(material))


def provider_name(item: dict) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]", "_", item["export_key"])[:48]
    return f"plugin__{UUID(item['id']).hex}__{safe}"


async def freeze_bindings(ids: list[str], *, kind: str, user_id: str, project_id: str) -> list[dict]:
    validate_binding_ids(ids)
    namespace = Namespace(user_id=user_id, project_id=project_id)
    result: list[dict] = []
    for identifier in ids:
        item = await resolve_binding(identifier, kind=kind, namespace=namespace)
        identity = binding_identity(item)
        snapshot = {"binding": item, "identity": identity.model_dump(mode="json")}
        if kind == "tool":
            binding = await _raw_bind(snapshot, ExecutionContext(user_id=user_id, project_id=project_id, identity=identity))
            snapshot["definition"] = binding.definition.model_dump(mode="json")
            snapshot["definition_digest"] = fingerprint(snapshot["definition"])
        result.append(snapshot)
    return result


async def revalidate(snapshot: dict, *, namespace: Namespace) -> None:
    item = snapshot["binding"]
    current = await resolve_binding(item["id"], kind=item["kind"], namespace=namespace)
    if binding_identity(current).model_dump(mode="json") != snapshot["identity"]:
        raise PluginError("plugin_configuration_changed")


async def _raw_bind(snapshot: dict, context: ExecutionContext) -> ToolBinding:
    item = snapshot["binding"]
    provider = get_plugin("tools", item["plugin_id"])
    spec = ToolSpec(binding_id=item["id"], export_key=item["export_key"], identity=PluginIdentity.model_validate(snapshot["identity"]), configuration=item["config"])
    binding = await provider.bind(spec, context, get_registry().host(item["plugin_id"]))
    definition = binding.definition.model_copy(update={"name": provider_name(item), "source": "plugin"})
    return replace(binding, definition=definition, config_fingerprint=spec.identity.config_fingerprint)


async def bind_tool(snapshot: dict, context: ExecutionContext) -> ToolBinding:
    namespace = Namespace(user_id=context.user_id, project_id=context.project_id)
    await revalidate(snapshot, namespace=namespace)
    bound_context = replace(context, identity=PluginIdentity.model_validate(snapshot["identity"]))
    binding = await _raw_bind(snapshot, bound_context)
    if fingerprint(binding.definition.model_dump(mode="json")) != snapshot["definition_digest"]:
        raise PluginError("plugin_configuration_changed")

    async def execute(arguments: dict, supplied: ExecutionContext):
        if (supplied.user_id, supplied.project_id, supplied.run_id) != (context.user_id, context.project_id, context.run_id):
            raise PluginError("plugin_authority_revoked")
        await revalidate(snapshot, namespace=namespace)
        current = await _raw_bind(snapshot, bound_context)
        if fingerprint(current.definition.model_dump(mode="json")) != snapshot["definition_digest"]:
            raise PluginError("plugin_configuration_changed")
        return await current.execute(arguments, replace(bound_context, call_id=supplied.call_id))

    return replace(binding, execute=execute)
