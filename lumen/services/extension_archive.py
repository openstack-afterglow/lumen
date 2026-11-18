"""Transactional recovery for extensions deduplicated by the scope backfill."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import BIGINT, DateTime, column, delete, select, table, update

from lumen.crypto import decrypt_extension_archive, encrypt_extension_archive
from lumen.db import get_session_factory
from lumen.models.chat_db import (
    ChatCustomTool,
    ChatExtensionArchive,
    ChatMcpActivation,
    ChatMcpOAuthConnection,
    ChatMcpOAuthRequest,
    ChatMcpServer,
)
from lumen.services.domain_scope import personal_mcp_scope_key, scope_clause

_MODELS = {"mcp": ChatMcpServer, "tool": ChatCustomTool}
_MCP_DEPENDENCIES = {
    "activations": ChatMcpActivation,
    "oauth_connections": ChatMcpOAuthConnection,
    "oauth_requests": ChatMcpOAuthRequest,
}
_STATE_TYPES = {
    "activation": (ChatMcpActivation, "generation", "activation"),
    "oauth_connection": (ChatMcpOAuthConnection, "credential_version", "OAuth credential"),
}
# Retired by the runtime, but old installs can still have recoverable ciphertext here.
_LEGACY_CREDENTIALS = table(
    "chat_mcp_credentials", column("id", BIGINT), column("mcp_server_id", BIGINT),
    column("owner_user_id"), column("owner_project_id"), column("encrypted_values"),
    column("created_at", DateTime()), column("updated_at", DateTime()),
)


class ArchiveNotFound(LookupError):
    pass


class ArchiveStorageUnavailable(RuntimeError):
    pass


def _factory():
    factory = get_session_factory()
    if factory is None:
        raise ArchiveStorageUnavailable("Chat storage unavailable")
    return factory


def _columns(row) -> dict:
    return {column.key: getattr(row, column.key) for column in row.__table__.columns}


def _serialize(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError("Unsupported archive field")


def _restore_types(columns, values: dict) -> dict:
    return {
        key: datetime.fromisoformat(value) if value is not None and isinstance(columns[key].type, DateTime) else value
        for key, value in values.items()
        if key in columns
    }


def archive_mcp_state(session, server, row, domain_id: str) -> None:
    entry_type = "activation" if isinstance(row, ChatMcpActivation) else "oauth_connection"
    label = f" ({_STATE_TYPES[entry_type][2]})"
    session.add(ChatExtensionArchive(
        kind="mcp", entry_type=entry_type, original_id=server.id, canonical_id=server.id, domain_id=domain_id,
        owner_user_id=row.owner_user_id, name=server.name[:100 - len(label)] + label,
        encrypted_snapshot=encrypt_extension_archive(json.dumps({"state": _columns(row)}, default=_serialize)),
    ))


async def archive_duplicate(session, kind: str, loser, winner_id: int, domain_id: str) -> list[dict]:
    """Save all original data before removing the live row, in the merge transaction."""
    snapshot = {"extension": _columns(loser)}
    if kind == "mcp":
        for key, model in _MCP_DEPENDENCIES.items():
            rows = (await session.scalars(
                select(model).where(model.mcp_server_id == loser.id).with_for_update()
            )).all()
            snapshot[key] = [_columns(row) for row in rows]
            for row in rows:
                await session.delete(row)
        legacy = _LEGACY_CREDENTIALS
        legacy_rows = await session.execute(select(legacy).where(legacy.c.mcp_server_id == loser.id).with_for_update())
        snapshot["legacy_credentials"] = [dict(row) for row in legacy_rows.mappings()]
        await session.execute(delete(legacy).where(legacy.c.mcp_server_id == loser.id))

    await session.execute(update(ChatExtensionArchive).where(
        ChatExtensionArchive.kind == kind,
        ChatExtensionArchive.entry_type == "extension",
        ChatExtensionArchive.canonical_id == loser.id,
        ChatExtensionArchive.restored_at.is_(None),
    ).values(canonical_id=winner_id))
    session.add(ChatExtensionArchive(
        kind=kind, original_id=loser.id, canonical_id=winner_id, domain_id=domain_id,
        owner_user_id=loser.owner_user_id if loser.scope == "user" else None,
        name=loser.name, encrypted_snapshot=encrypt_extension_archive(json.dumps(snapshot, default=_serialize)),
    ))
    await session.flush()
    await session.delete(loser)
    await session.flush()  # Release the loser's scope/template uniqueness slot before promoting the winner.
    return snapshot.get("activations", [])


def _visible(user_id: str | None, domain_id: str):
    return (
        ChatExtensionArchive.owner_user_id == user_id,
        ChatExtensionArchive.domain_id == domain_id,
        ChatExtensionArchive.restored_at.is_(None),
    )


async def list_archives(*, user_id: str | None, domain_id: str, limit: int, before_id: int | None) -> list[dict]:
    async with _factory()() as session:
        query = select(
            ChatExtensionArchive.id, ChatExtensionArchive.kind, ChatExtensionArchive.original_id,
            ChatExtensionArchive.canonical_id, ChatExtensionArchive.name, ChatExtensionArchive.archived_at,
        ).where(*_visible(user_id, domain_id))
        if before_id is not None:
            query = query.where(ChatExtensionArchive.id < before_id)
        rows = await session.execute(query.order_by(ChatExtensionArchive.id.desc()).limit(limit))
        return [dict(row) for row in rows.mappings()]


async def restore_archive(*, archive_id: int, user_id: str | None, domain_id: str) -> dict:
    async with _factory()() as session, session.begin():
        archive = await session.scalar(select(ChatExtensionArchive).where(
            ChatExtensionArchive.id == archive_id, *_visible(user_id, domain_id),
        ).with_for_update())
        if archive is None:
            raise ArchiveNotFound("Archived connection not found")
        snapshot = json.loads(decrypt_extension_archive(archive.encrypted_snapshot))
        if archive.entry_type != "extension":
            await _restore_mcp_state(session, archive, snapshot["state"], user_id, domain_id)
            archive.restored_at = datetime.now(UTC)
            return {"kind": "mcp", "id": archive.original_id}
        model = _MODELS[archive.kind]
        fields = _restore_types(model.__table__.columns, snapshot["extension"])
        owner_matches = (
            fields["scope"] == "user" and fields["owner_user_id"] == user_id
            if user_id is not None else fields["scope"] == "domain" and fields["owner_domain_id"] == domain_id
        )
        if fields["id"] != archive.original_id or not owner_matches:
            raise ArchiveNotFound("Archived connection identity mismatch")

        if model is ChatMcpServer:
            fields["owner_domain_id"] = domain_id
            if user_id is not None:
                fields["connection_scope_key"] = personal_mcp_scope_key(user_id, fields["owner_project_id"], domain_id)
            await _label_restored_mcp(session, fields)
        elif user_id is not None:
            fields["domain_id"] = domain_id
            await _name_restored_tool(session, fields)
        session.add(model(**fields))
        await session.flush()
        if model is ChatMcpServer:
            await _restore_mcp_dependencies(session, snapshot, fields, user_id, domain_id)
        archive.restored_at = datetime.now(UTC)
        return {"kind": archive.kind, "id": archive.original_id}


async def _restore_mcp_state(session, archive, raw: dict, user_id: str | None, domain_id: str) -> None:
    model, version_key, _ = _STATE_TYPES[archive.entry_type]
    fields = _restore_types(model.__table__.columns, raw)
    server = await session.get(ChatMcpServer, archive.original_id, with_for_update=True)
    if (user_id is None or fields["owner_user_id"] != user_id or fields["mcp_server_id"] != archive.original_id
            or fields["owner_domain_id"] not in {None, domain_id} or server is None
            or (server.scope == "user" and (server.owner_domain_id != domain_id or server.owner_user_id != user_id))
            or (server.scope == "domain" and (server.owner_domain_id != domain_id or model is ChatMcpOAuthConnection))
            or server.scope not in {"global", "domain", "user"}):
        raise ArchiveNotFound("Archived connection state cannot be restored")
    live = (await session.scalars(select(model).where(
        model.mcp_server_id == server.id, model.owner_user_id == user_id,
        scope_clause(model.owner_domain_id, model.owner_project_id,
                     domain_id=domain_id, project_id=fields["owner_project_id"]),
    ).with_for_update())).all()
    for row in live:
        archive_mcp_state(session, server, row, domain_id)
        fields[version_key] = max(fields[version_key], getattr(row, version_key))
        await session.delete(row)
    await session.flush()
    fields[version_key] += 1
    fields["owner_domain_id"] = domain_id
    session.add(model(**fields))


async def _label_restored_mcp(session, fields: dict) -> None:
    label = fields["connection_label"] or fields["name"]
    scope_key = fields["connection_scope_key"]
    if await session.scalar(select(ChatMcpServer.id).where(
        ChatMcpServer.connection_scope_key == scope_key,
        ChatMcpServer.template_key == fields["template_key"],
    ).where((ChatMcpServer.connection_label == label) | (
        ChatMcpServer.connection_label.is_(None) & (ChatMcpServer.name == label)
    )).limit(1)) is not None:
        suffix = f" (restored {fields['id']})"
        fields["connection_label"] = label[:100 - len(suffix)] + suffix


async def _name_restored_tool(session, fields: dict) -> None:
    if await session.scalar(select(ChatCustomTool.id).where(
        ChatCustomTool.scope == "user", ChatCustomTool.owner_user_id == fields["owner_user_id"],
        ChatCustomTool.domain_id == fields["domain_id"], ChatCustomTool.name == fields["name"],
    ).limit(1)) is not None:
        suffix = f"_restored_{fields['id']}"
        fields["name"] = fields["name"][:64 - len(suffix)] + suffix


async def _restore_mcp_dependencies(session, snapshot: dict, extension: dict, user_id: str | None, domain_id: str) -> None:
    origin = extension["owner_project_id"]
    for key, model in _MCP_DEPENDENCIES.items():
        items = [_restore_types(model.__table__.columns, row) for row in snapshot[key]]
        if user_id is not None and key != "oauth_requests":
            # Only the archived owner's exact approved origin/domain can regain authority.
            items = [item for item in items if item["owner_user_id"] == user_id and (
                item["owner_domain_id"] == domain_id or (
                    item["owner_domain_id"] is None and item["owner_project_id"] == origin
                )
            )]
            items = sorted(items, key=lambda item: (item["updated_at"], item["owner_project_id"]), reverse=True)[:1]
            for item in items:
                item["owner_domain_id"] = domain_id
        for item in items:
            if key == "oauth_requests" and item["status"] in {"pending", "processing"}:
                item["status"] = "expired"
            session.add(model(**item))
    for raw in snapshot["legacy_credentials"]:
        await session.execute(_LEGACY_CREDENTIALS.insert().values(**_restore_types(_LEGACY_CREDENTIALS.c, raw)))
