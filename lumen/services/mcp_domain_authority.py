"""Fresh native domain membership and MCP catalog management authority.

The canonical Default project's legitimate managers manage an Afterglow domain.
The managed reader projection is derived access, never an independent grant.
No caller token or secret enters this resolver or its result.
"""

from __future__ import annotations

import logging
from typing import TypedDict
from urllib.parse import quote, unquote, urlencode, urlsplit

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from lumen import auth

_logger = logging.getLogger(__name__)
_PROJECTION_PREFIX = "afterglow-default-managers-"
_PROJECTION_MARKER = "afterglow:default-manager-reader:v1:"


class DomainAuthority(TypedDict):
    domain_id: str
    is_member: bool
    can_manage: bool


def _enabled_exact(manager, object_id: str):
    from keystoneclient.exceptions import NotFound

    try:
        row = manager.get(object_id)
    except NotFound as exc:
        raise HTTPException(403, "Current Keystone domain identity no longer exists") from exc
    if row.id != object_id or not isinstance(getattr(row, "enabled", None), bool):
        raise ValueError("Current Keystone domain identity unavailable")
    if not row.enabled:
        raise HTTPException(403, "Current Keystone domain identity is disabled")
    return row


def _role_ids(
    ks, user_id: str, project_id: str, graph: dict[str, set[str]], *, excluded_groups: set[str] | None = None,
) -> set[str]:
    rows = auth._identity_collection(ks, "/role_assignments?" + urlencode({
        "user.id": user_id, "scope.project.id": project_id, "effective": True,
    }), "role_assignments")
    result = set()
    for row in rows:
        scope, subject = row.get("scope"), row.get("user")
        if (not isinstance(scope, dict) or not isinstance(subject, dict)
                or scope.get("project", {}).get("id") != project_id
                or subject.get("id") != user_id or row.get("group") is not None):
            raise ValueError("Current Keystone effective project assignment mismatch")
        rid = row.get("role", {}).get("id")
        if excluded_groups is not None:
            links = row.get("links", {})
            if not isinstance(links, dict):
                raise ValueError("Current Keystone effective assignment links unavailable")
            membership = links.get("membership")
            if membership:
                if not isinstance(membership, str):
                    raise ValueError("Current Keystone effective group provenance unavailable")
                parts = urlsplit(membership).path.split("/")
                if "groups" not in parts or parts.index("groups") + 1 >= len(parts):
                    raise ValueError("Current Keystone effective group provenance unavailable")
                group_id = unquote(parts[parts.index("groups") + 1])
                if not group_id:
                    raise ValueError("Current Keystone effective group provenance unavailable")
                if group_id in excluded_groups:
                    continue
        if not isinstance(rid, str) or rid not in graph:
            raise ValueError("Current Keystone role identity unavailable")
        result.add(rid)
    return result


def _default_project(ks, domain_id: str):
    matches = []
    for row in auth._identity_collection(ks, "/projects?" + urlencode({"domain_id": domain_id}), "projects"):
        if row.get("domain_id") != domain_id or not isinstance(row.get("name"), str):
            raise ValueError("Current Keystone domain project list mismatch")
        if row["name"].strip().casefold() == "default":
            if row["name"] != "Default" or not isinstance(row.get("id"), str) or not row["id"]:
                raise ValueError("Canonical Default project is ambiguous")
            matches.append(row["id"])
    if len(matches) > 1:
        raise ValueError("Canonical Default project is ambiguous")
    if not matches:
        return None
    project = _enabled_exact(ks.projects, matches[0])
    if getattr(project, "domain_id", None) != domain_id or getattr(project, "name", None) != "Default":
        raise ValueError("Canonical Default project identity mismatch")
    return project


def _ancestors(ks, project, domain_id: str) -> set[str]:
    result = set()
    pending = getattr(project, "parent_id", None)
    while pending is not None and pending != domain_id:
        if not isinstance(pending, str) or not pending or pending in result or pending == project.id:
            raise ValueError("Current Keystone project ancestry unavailable")
        parent = _enabled_exact(ks.projects, pending)
        if getattr(parent, "domain_id", None) != domain_id:
            raise ValueError("Current Keystone project ancestry crosses domains")
        result.add(pending)
        pending = getattr(parent, "parent_id", None)
    return result


def _manager(ks, user_id: str, domain_id: str, graph: dict[str, set[str]], global_ids: dict[str, str]) -> bool:
    manager_id = global_ids.get("manager")
    if manager_id is None:
        return False
    default = _default_project(ks, domain_id)
    projection_groups = set()
    ancestors = _ancestors(ks, default, domain_id) if default is not None else set()
    subjects = [("user", user_id)]
    for group in auth._identity_collection(ks, f"/users/{quote(user_id, safe='')}/groups", "groups"):
        gid = group.get("id")
        if not isinstance(gid, str) or not gid:
            raise ValueError("Current Keystone group identity unavailable")
        native = ks.groups.get(gid)
        if native.id != gid:
            raise ValueError("Current Keystone group identity mismatch")
        name, description = getattr(native, "name", ""), getattr(native, "description", "")
        if not isinstance(name, str) or not isinstance(description, str):
            raise ValueError("Current Keystone group metadata unavailable")
        if name.startswith(_PROJECTION_PREFIX) or description.startswith(_PROJECTION_MARKER):
            projection_groups.add(gid)
            continue
        subjects.append(("group", gid))
    direct_domain = set()
    default_sources = set()
    for kind, subject_id in subjects:
        rows = auth._identity_collection(ks, "/role_assignments?" + urlencode({f"{kind}.id": subject_id}),
                                         "role_assignments")
        for row in rows:
            subject, scope, role = row.get(kind), row.get("scope"), row.get("role")
            other = "group" if kind == "user" else "user"
            if (not isinstance(subject, dict) or subject.get("id") != subject_id
                    or row.get(other) is not None or not isinstance(scope, dict)
                    or not isinstance(role, dict) or role.get("id") not in graph):
                raise ValueError("Current Keystone raw role assignment unavailable")
            scopes = set(scope) - {"OS-INHERIT:inherited_to"}
            if len(scopes) != 1 or not scopes.issubset({"project", "domain", "system"}):
                raise ValueError("Current Keystone raw role scope unavailable")
            scope_kind = next(iter(scopes))
            inherited = "OS-INHERIT:inherited_to" in scope
            if inherited and (scope_kind == "system" or scope["OS-INHERIT:inherited_to"] != "projects"):
                raise ValueError("Current Keystone inheritance unavailable")
            value = scope[scope_kind]
            if not isinstance(value, dict):
                raise ValueError("Current Keystone raw role scope unavailable")
            if scope_kind == "system":
                if set(value) != {"all"} or value["all"] is not True:
                    raise ValueError("Current Keystone system scope unavailable")
                continue
            scope_id = value.get("id")
            if not isinstance(scope_id, str) or not scope_id:
                raise ValueError("Current Keystone raw role scope unavailable")
            if scope_kind == "domain" and scope_id == domain_id and not inherited:
                direct_domain.add(role["id"])
            if default is not None and (
                (scope_kind == "project" and (
                    (scope_id == default.id and not inherited) or (scope_id in ancestors and inherited)
                )) or (scope_kind == "domain" and scope_id == domain_id and inherited)
            ):
                default_sources.add(role["id"])
    domain_manager = manager_id in auth._expand_role_ids(direct_domain, graph)
    if default is None:
        return domain_manager
    legitimate = manager_id in auth._expand_role_ids(default_sources, graph)
    effective = manager_id in auth._expand_role_ids(
        _role_ids(ks, user_id, default.id, graph, excluded_groups=projection_groups), graph,
    )
    # Keystone's effective listing must agree with independent native provenance.
    # A projection-only manager or incomplete/racing directory read fails closed.
    if legitimate != effective:
        raise ValueError("Canonical Default manager provenance mismatch")
    return domain_manager or legitimate


def _resolve_domain_authority(
    user_id: str, project_id: str, domain_id: str | None, *, is_system_admin: bool,
    issued_role_ids: tuple[str, ...] | None, compute_manage: bool = True,
) -> DomainAuthority:
    try:
        ks = auth._get_admin_ks_client()
        user = _enabled_exact(ks.users, user_id)
        project = _enabled_exact(ks.projects, project_id)
        project_domain = getattr(project, "domain_id", None)
        home_domain = getattr(user, "domain_id", None)
        if any(not isinstance(value, str) or not value for value in (project_domain, home_domain)):
            raise ValueError("Current Keystone native domain identity unavailable")
        target = project_domain if domain_id is None else domain_id
        if not isinstance(target, str) or not target:
            raise HTTPException(403, "Exact domain identity required")
        if target != project_domain and not is_system_admin:
            raise HTTPException(403, "Domain must match the authenticated target project")
        domains = {
            native_id: _enabled_exact(ks.domains, native_id)
            for native_id in {home_domain, project_domain, target}
        }
        if is_system_admin:
            return {"domain_id": target, "is_member": True, "can_manage": True}
        graph, global_ids = auth._load_role_graph(ks)
        current = _role_ids(ks, user_id, project_id, graph)
        if issued_role_ids is not None:
            current.intersection_update(issued_role_ids)
        if not current:
            raise HTTPException(403, "Current project membership required")
        target_domain = domains[target]
        name = str(getattr(target_domain, "name", "")).casefold()
        marker = getattr(target_domain, "afterglow_domain_kind", None)
        restricted_domain = name in {"accounts", "account", "personal"} or marker in {"account", "personal"}
        return {
            "domain_id": target,
            "is_member": True,
            "can_manage": (
                compute_manage and issued_role_ids is None and not restricted_domain
                and _manager(ks, user_id, target, graph, global_ids)
            ),
        }
    except HTTPException:
        raise
    except Exception as exc:
        _logger.warning("Current Keystone domain authority lookup failed", exc_info=True)
        raise HTTPException(503, "Current Keystone domain authority unavailable") from exc


async def resolve_domain_authority(
    user_id: str, project_id: str, domain_id: str | None = None, *, credential_authority: dict | None = None,
    compute_manage: bool = True,
) -> DomainAuthority:
    """Resolve exact domain access using fresh identity and native role provenance.

    Ordinary callers stay in their authenticated target project's live domain.
    Only unrestricted human system administrators may select another domain.
    Reduced credentials keep their issued native-ID membership ceiling and can
    never manage domain state, including when their owner is now an administrator.
    """
    current = await auth.resolve_project_authority(user_id, project_id, credential_authority=credential_authority)
    issued = None if credential_authority is None else auth._credential_role_ids(
        credential_authority, user_id, project_id,
    )
    return await run_in_threadpool(
        _resolve_domain_authority, user_id, project_id, domain_id,
        is_system_admin=credential_authority is None and current["is_system_admin"] is True,
        issued_role_ids=issued, compute_manage=compute_manage,
    )


async def project_domain_id(project_id: str) -> str:
    """The enabled project's live Keystone domain id: one directory read, no membership or role evaluation.

    For callers that already verified membership elsewhere and only need to locate shared domain state.
    """
    def read() -> str:
        ks = auth._get_admin_ks_client()
        project = _enabled_exact(ks.projects, project_id)
        domain_id = getattr(project, "domain_id", None)
        if not isinstance(domain_id, str) or not domain_id:
            raise ValueError("Current Keystone native domain identity unavailable")
        return domain_id

    return await run_in_threadpool(read)
