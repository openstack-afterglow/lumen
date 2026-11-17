"""Catalog-backed personal and exact-domain MCP settings; public views contain no secrets."""
from __future__ import annotations

import functools
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from lumen.auth import credential_authority_snapshot
from lumen.models.chat_db import ChatMcpOAuthConnection, ChatMcpServer
from lumen.services import extensions_store as es
from lumen.services import mcp_connections
from lumen.services import mcp_registry_catalog as catalog
from lumen.services.mcp_domain_authority import resolve_domain_authority
from lumen.services.mcp_registry_model import AUTH_MODES, McpRegistryError


def managed_profiles() -> dict[str, dict]:
    """Sandbox-hosted stdio profiles the running configuration can actually bind (no secrets, names only)."""
    from lumen.config import get_settings
    from lumen.services.execution_protocol import v2_runtime_ready
    from lumen.services.infrastructure.tool_manifest import ToolManifestError, load_tool_manifest

    settings = get_settings()
    runtime = getattr(settings, "runtime_config", None)
    if runtime is None or not runtime.enabled:
        return {}
    result: dict[str, dict] = {}
    protocol_ready = v2_runtime_ready(getattr(settings, "chat_execution_protocol_version", 1))
    for pool in runtime.pools:
        if not (pool.enabled and pool.role == "sandbox" and pool.backend == "kubernetes"):
            continue
        try:
            profiles = load_tool_manifest(pool.profile.tool_manifest_file, pool.profile.tool_manifest_sha256)
        except ToolManifestError:
            continue
        for profile in profiles:
            result.setdefault(profile.id, {
                "available": protocol_ready, "reason": None if protocol_ready else "execution_protocol_v2_unavailable",
                "pool": pool.name, "tools": sorted(tool.name for tool in profile.tools),
            })
    return result


def human(principal: dict) -> bool:
    """Unrestricted human Keystone session: the only actor that may manage organization state or reach other domains."""
    return principal.get("auth_type") == "keystone" and not principal.get("credential_restricted")


@contextmanager
def actor(principal: dict) -> Iterator[None]:
    """Bind the request's humanness for every store/OAuth domain check beneath it."""
    with es.actor_scope(human=human(principal)):
        yield


def _as_actor(func):
    @functools.wraps(func)
    async def wrapper(principal, *args, **kwargs):
        with actor(principal):
            return await func(principal, *args, **kwargs)

    return wrapper


async def authority(
    principal: dict, domain_id: str | None = None, *, manage: bool = False, hint: bool = False,
) -> dict:
    """Membership always; the (expensive) manager evaluation only when mutating or when a UI hint is requested.

    System-admin cross-domain reach and the manager flag exist only for unrestricted human sessions; API keys and
    reduced credentials get plain exact-domain membership of their own project's domain.
    """
    is_human = human(principal)
    result = await resolve_domain_authority(
        principal["user_id"], principal["project_id"], domain_id,
        credential_authority=credential_authority_snapshot(principal),
        compute_manage=(manage or hint) and is_human, unrestricted_human=is_human,
    )
    if not result["is_member"] or (manage and (not result["can_manage"] or not is_human)):
        raise es.ExtensionForbidden("Exact domain manager authority required" if manage else "Domain membership required")
    return result


async def _caller_effective(principal: dict, domain_id: str, item_id: int | None = None) -> list[dict]:
    """Organization rows as the caller sees them: ``is_active`` needs publication + the caller's own activation of
    the exact config_version + a connected shared credential (same rule as ``list_for_user``)."""
    try:
        async with es._require_db()() as session:
            query = select(ChatMcpServer).where(
                ChatMcpServer.scope == "domain", ChatMcpServer.owner_domain_id == domain_id,
            ).order_by(ChatMcpServer.id)
            if item_id is not None:
                query = query.where(ChatMcpServer.id == item_id)
            rows = (await session.execute(query)).scalars().all()
            activations = await es._owner_activations(session, principal["user_id"], principal["project_id"])
            shared = {
                row.mcp_server_id: row for row in (await session.execute(select(ChatMcpOAuthConnection).where(
                    ChatMcpOAuthConnection.owner_user_id == domain_id,
                    ChatMcpOAuthConnection.owner_project_id == domain_id,
                ))).scalars()
            }
            result = []
            for row in rows:
                activation = activations.get(row.id)
                connected = es._mcp_connected(row, shared.get(row.id))
                item = es._public_mcp(row)
                item.update(es._activation_metadata(row, activation))
                item["is_active"] = es._mcp_effectively_active(row, activation) and connected
                item["auth_connected"] = connected
                result.append(item)
            return result
    except OperationalError as exc:
        raise es.ChatStorageUnavailable("chat DB 오류") from exc


@_as_actor
async def domain_connections(principal: dict, domain_id: str) -> list[dict]:
    await authority(principal, domain_id)
    return await _caller_effective(principal, domain_id)


@_as_actor
async def scoped_connection(principal: dict, item_id: int, domain_id: str | None = None, *, manage: bool = False) -> dict:
    if domain_id is not None:
        await authority(principal, domain_id, manage=manage)
    try:
        async with es._require_db()() as session:
            row = await session.get(ChatMcpServer, item_id)
            if row is None:
                raise es.ExtensionNotFound("MCP connection not found")
            if domain_id is not None:
                if row.scope != "domain" or row.owner_domain_id != domain_id:
                    raise es.ExtensionNotFound("MCP connection not found")
            elif row.scope != "user" or (
                row.owner_user_id, row.owner_project_id
            ) != (principal["user_id"], principal["project_id"]):
                raise es.ExtensionForbidden("Personal connection ownership required")
            return es._public_mcp(row)
    except OperationalError as exc:
        raise es.ChatStorageUnavailable("chat DB 오류") from exc


@_as_actor
async def create(principal: dict, template_id: str, *, domain_id: str | None = None,
                 label: str | None = None, variables: dict[str, str] | None = None,
                 auth_mode: str | None = None, headers: dict[str, str] | None = None) -> dict:
    if domain_id is not None:
        await authority(principal, domain_id, manage=True)
    candidate = catalog.entry(template_id)
    access = candidate["access"]
    if access not in {"open", "oauth", "credentials", "configure"}:
        raise McpRegistryError("This template has no verified usable hosted MCP endpoint")
    mode = {"open": "none", "oauth": "oauth", "credentials": "admin"}.get(access, auth_mode)
    if mode not in AUTH_MODES or (auth_mode is not None and auth_mode != mode):
        raise McpRegistryError("auth_mode does not match the template")
    if (mode == "admin") != bool(headers):
        raise McpRegistryError("Static credentials are required only for auth_mode=admin")
    _check_header_names(candidate, headers)
    url = catalog._fill_url(candidate, variables)
    connection_label = mcp_connections._label(label, candidate["name"])
    scope = "domain" if domain_id is not None else "user"
    scope_key = f"domain:{domain_id}" if domain_id is not None else f"user:{principal['user_id']}:{principal['project_id']}"
    factory = es._require_db()
    try:
        async with factory() as session:
            existing = (await session.execute(select(ChatMcpServer).where(
                ChatMcpServer.connection_scope_key == scope_key,
                ChatMcpServer.template_key == template_id, ChatMcpServer.connection_label == connection_label,
            ))).scalar_one_or_none()
            if existing is not None:
                return {"created": False, "server": await _view(principal, existing.id, domain_id, es._public_mcp(existing))}
        server = await es.create("mcp", {
            "name": candidate["name"], "url": url, "transport": candidate["transport"],
            "auth_mode": mode, **({"headers": headers} if headers else {}),
            "template_key": template_id, "connection_label": connection_label, "implicit_selection": False,
        }, scope=scope, owner_user_id=principal["user_id"], owner_project_id=principal["project_id"],
            owner_domain_id=domain_id, catalog_authorized=True)
        return {"created": True, "server": await _view(principal, server["id"], domain_id, server)}
    except (OperationalError, IntegrityError) as exc:
        raise es.ChatStorageUnavailable("chat DB 오류") from exc


def _check_header_names(candidate: dict, headers: dict[str, str] | None) -> None:
    """Static credentials may only use header names the registry declares for the template (when it names any)."""
    allowed = {str(hint["name"]).casefold() for hint in candidate.get("header_hints") or [] if hint.get("name")}
    unknown = sorted(name for name in (headers or {}) if allowed and name.casefold() not in allowed)
    if unknown:
        raise McpRegistryError("이 템플릿이 선언한 헤더 이름만 사용할 수 있습니다: " + ", ".join(sorted(allowed)))


@_as_actor
async def update(principal: dict, item_id: int, patch: dict, *, domain_id: str | None = None) -> dict:
    row = await scoped_connection(principal, item_id, domain_id, manage=True)
    if patch.get("headers") and row.get("template_key"):
        _check_header_names(catalog.entry(row["template_key"]), patch["headers"])
    if domain_id is None:
        # Personal static credentials are accepted only for the selected catalog template.
        candidate = catalog.entry(row["template_key"])
        if "headers" in patch and candidate["access"] not in {"credentials", "configure"}:
            raise McpRegistryError("This template does not accept static credentials")
        if set(patch) - {"headers", "is_active"}:
            raise es.ExtensionValidationError("Personal connection patch accepts headers and is_active only")
    updated = await es.update("mcp", item_id, patch, requester_user_id=principal["user_id"],
                              requester_project_id=principal["project_id"], domain_id=domain_id,
                              catalog_authorized=True)
    return await _view(principal, item_id, domain_id, updated)


async def _view(principal: dict, item_id: int, domain_id: str | None, fallback: dict) -> dict:
    """Organization mutations answer with the caller-effective row, like the list."""
    if domain_id is None:
        return fallback
    rows = await _caller_effective(principal, domain_id, item_id)
    return rows[0] if rows else fallback


@_as_actor
async def delete(principal: dict, item_id: int, *, domain_id: str | None = None) -> None:
    await scoped_connection(principal, item_id, domain_id, manage=True)
    await es.delete("mcp", item_id, requester_user_id=principal["user_id"],
                    requester_project_id=principal["project_id"], domain_id=domain_id)


@_as_actor
async def view(
    principal: dict, *, offset: int = 0, limit: int = 50, q: str | None = None,
    featured: bool | None = None, access: str | None = None, state: str | None = None,
) -> dict:
    """Per-user catalog view. Filters apply before pagination: ``q`` matches name/registry_name/description
    case-insensitively; ``state`` filters the computed per-user state."""
    try:
        owner = await authority(principal, hint=True)
        organization_available = True
    except (HTTPException, es.ExtensionForbidden):
        # Personal and global entries stay usable; organization rows are omitted by list_for_user the same way.
        owner = {"domain_id": None, "can_manage": False}
        organization_available = False
    rows = await es.list_for_user("mcp", user_id=principal["user_id"], project_id=principal["project_id"])
    linked: dict[str, list[dict]] = {}
    for row in rows:
        connected = row.get("auth_connected", row["auth_mode"] != "oauth")
        # Connected only counts once the row is also effectively usable by this caller (published + activated).
        row_state = "needs_auth" if not connected else (
            "not_connected" if not row["is_active"]
            else "personal_connected" if row["scope"] == "user"
            else "org_connected" if row["scope"] == "domain" else "global_connected"
        )
        linked.setdefault(row.get("template_key") or "", []).append({**row, "state": row_state})
    needle = (q or "").strip().casefold()
    managed = managed_profiles()
    selected: list[dict] = []
    for entry in catalog.entries():
        if needle and not any(
            needle in str(entry.get(key) or "").casefold() for key in ("name", "registry_name", "description")
        ):
            continue
        if featured is not None and bool(entry.get("featured")) != featured:
            continue
        if access is not None and entry.get("access") != access:
            continue
        connections = linked.get(entry["id"], [])
        states = {row["state"] for row in connections}
        entry_state = next((value for value in ("personal_connected", "org_connected", "global_connected", "needs_auth")
                            if value in states), "not_connected")
        item = {**entry, "state": entry_state, "connections": connections, "managed": None}
        if entry.get("access") == "managed":
            profile = managed.get(entry.get("managed_profile") or "")
            available = bool(profile and profile["available"])
            item["state"] = "managed_available" if available else "managed_unavailable"
            item["managed"] = {
                "profile": entry.get("managed_profile"), "available": available,
                "reason": None if available else (
                    profile["reason"] if profile else "profile_not_in_enabled_sandbox_manifest"),
                "tools": profile["tools"] if profile else [],
            }
        if state is not None and item["state"] != state:
            continue
        selected.append(item)
    page = selected[offset:offset + limit]
    return {"entries": page, "total": len(selected), "offset": offset, "limit": limit,
            "next_offset": offset + limit if offset + limit < len(selected) else None,
            "domain_id": owner["domain_id"], "can_manage_domain": owner["can_manage"],
            "organization_available": organization_available,
            "custom_connections": linked.get("", [])}


@_as_actor
async def check_oauth_management(principal: dict, item_id: int) -> None:
    """Keep legacy OAuth routes from granting reduced callers (or API keys) organization management.

    Fails closed: a storage outage answers 503 instead of skipping the check, and the SQL OAuth store re-checks with
    the same request actor (not human => no management), so no path reaches a revocation without both.
    """
    try:
        async with es._require_db()() as session:
            row = await session.get(ChatMcpServer, item_id)
            domain_id = row.owner_domain_id if row is not None and row.scope == "domain" else None
        if domain_id is not None:
            await authority(principal, domain_id, manage=True)
    except OperationalError as exc:
        raise es.ChatStorageUnavailable("chat DB 오류") from exc
