"""Catalog-backed personal and exact-domain MCP settings; public views contain no secrets."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from lumen.auth import credential_authority_snapshot
from lumen.models.chat_db import ChatMcpServer
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


async def authority(principal: dict, domain_id: str | None = None, *, manage: bool = False) -> dict:
    result = await resolve_domain_authority(
        principal["user_id"], principal["project_id"], domain_id,
        credential_authority=credential_authority_snapshot(principal),
    )
    if not result["is_member"] or (manage and (
        not result["can_manage"] or principal.get("auth_type") != "keystone"
        or principal.get("credential_restricted")
    )):
        raise es.ExtensionForbidden("Exact domain manager authority required" if manage else "Domain membership required")
    return result


async def domain_connections(principal: dict, domain_id: str) -> list[dict]:
    await authority(principal, domain_id)
    try:
        async with es._require_db()() as session:
            rows = (await session.execute(select(ChatMcpServer).where(
                ChatMcpServer.scope == "domain", ChatMcpServer.owner_domain_id == domain_id,
            ).order_by(ChatMcpServer.id))).scalars()
            return [es._public_mcp(row) for row in rows]
    except OperationalError as exc:
        raise es.ChatStorageUnavailable("chat DB 오류") from exc


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
                return {"created": False, "server": es._public_mcp(existing)}
        server = await es.create("mcp", {
            "name": candidate["name"], "url": url, "transport": candidate["transport"],
            "auth_mode": mode, **({"headers": headers} if headers else {}),
            "template_key": template_id, "connection_label": connection_label, "implicit_selection": False,
        }, scope=scope, owner_user_id=principal["user_id"], owner_project_id=principal["project_id"],
            owner_domain_id=domain_id, catalog_authorized=True)
        return {"created": True, "server": server}
    except (OperationalError, IntegrityError) as exc:
        raise es.ChatStorageUnavailable("chat DB 오류") from exc


async def update(principal: dict, item_id: int, patch: dict, *, domain_id: str | None = None) -> dict:
    row = await scoped_connection(principal, item_id, domain_id, manage=True)
    if domain_id is None:
        # Personal static credentials are accepted only for the selected catalog template.
        candidate = catalog.entry(row["template_key"])
        if "headers" in patch and candidate["access"] not in {"credentials", "configure"}:
            raise McpRegistryError("This template does not accept static credentials")
        if set(patch) - {"headers", "is_active"}:
            raise es.ExtensionValidationError("Personal connection patch accepts headers and is_active only")
    return await es.update("mcp", item_id, patch, requester_user_id=principal["user_id"],
                           requester_project_id=principal["project_id"], domain_id=domain_id, catalog_authorized=True)


async def delete(principal: dict, item_id: int, *, domain_id: str | None = None) -> None:
    await scoped_connection(principal, item_id, domain_id, manage=True)
    await es.delete("mcp", item_id, requester_user_id=principal["user_id"],
                    requester_project_id=principal["project_id"], domain_id=domain_id)


async def view(
    principal: dict, *, offset: int = 0, limit: int = 50, q: str | None = None,
    featured: bool | None = None, access: str | None = None, state: str | None = None,
) -> dict:
    """Per-user catalog view. Filters apply before pagination: ``q`` matches name/registry_name/description
    case-insensitively; ``state`` filters the computed per-user state."""
    owner = await authority(principal)
    rows = await es.list_for_user("mcp", user_id=principal["user_id"], project_id=principal["project_id"])
    linked: dict[str, list[dict]] = {}
    for row in rows:
        connected = row.get("auth_connected", row["auth_mode"] != "oauth")
        row_state = "needs_auth" if not connected else (
            "personal_connected" if row["scope"] == "user" else "org_connected" if row["scope"] == "domain"
            else "global_connected" if row["is_active"] else "not_connected"
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
            "custom_connections": linked.get("", [])}


async def check_oauth_management(principal: dict, item_id: int) -> None:
    """Keep legacy OAuth routes from granting reduced callers organization management.

    A storage outage skips this pre-check only: the SQL OAuth store re-checks domain manager authority on the
    same database before any state, token or revocation is written, so the request still fails closed.
    """
    try:
        async with es._require_db()() as session:
            row = await session.get(ChatMcpServer, item_id)
            domain_id = row.owner_domain_id if row is not None and row.scope == "domain" else None
        if domain_id is not None:
            await authority(principal, domain_id, manage=True)
    except es.ChatStorageUnavailable:
        return
    except OperationalError as exc:
        raise es.ChatStorageUnavailable("chat DB 오류") from exc
