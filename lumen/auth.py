"""Current caller authority, directory reads and caller-token OpenStack connections."""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict
from urllib.parse import quote, urlencode

from fastapi import Depends, Header, HTTPException, Request, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from starlette.concurrency import run_in_threadpool

from lumen.config import get_settings
from lumen.service_authority import SERVICE_CAPABILITIES

keystone_token_scheme = APIKeyHeader(name="X-Auth-Token", auto_error=False, scheme_name="KeystoneToken")
keystone_bearer_scheme = HTTPBearer(auto_error=False, scheme_name="KeystoneBearer")

api_key_bearer_scheme = HTTPBearer(auto_error=False, scheme_name="APIKeyBearer")
x_api_key_scheme = APIKeyHeader(name="x-api-key", auto_error=False, scheme_name="XApiKey")

_logger = logging.getLogger(__name__)


class Principal(TypedDict):
    auth_type: Literal["keystone", "api_key"]
    user_id: str
    project_id: str
    connection_project_id: NotRequired[str]
    connection_system_scope: NotRequired[str]
    api_key_id: int | None
    source_project_id: NotRequired[str]
    source_system_scope: NotRequired[str]
    credential_restricted: NotRequired[bool]
    issued_role_ids: NotRequired[tuple[str, ...]]
    scopes: tuple[str, ...]
    source: Literal["web", "api"]
    roles: list[str]
    is_system_admin: bool
    service_system_admin: NotRequired[bool]
    personal_project_owner: NotRequired[bool]
    credential_kind: NotRequired[str]
    expires_at: NotRequired[object]
    token: NotRequired[str]


@dataclass
class CacheMode:
    enabled: bool = True
    refresh: bool = False


def cache_mode(
    no_cache: bool = False,
    refresh_cache: bool = False,
) -> CacheMode:
    return CacheMode(enabled=not no_cache, refresh=refresh_cache)


# Directory reads gate provider I/O; a stalled Keystone must surface as an
# authority outage (503), never as an indefinitely parked worker or request.
_KEYSTONE_ADMIN_TIMEOUT_SECONDS = 15


def _get_admin_ks_client():
    from keystoneauth1 import session as ks_session
    from keystoneauth1.identity import v3
    from keystoneclient.v3 import client as ks_client

    settings = get_settings()
    auth = v3.Password(
        auth_url=settings.keystone_auth_url,
        username=settings.keystone_admin_username,
        password=settings.keystone_admin_password,
        project_name=settings.keystone_admin_project,
        user_domain_name=settings.keystone_domain,
        project_domain_name=settings.keystone_domain,
    )
    session = ks_session.Session(auth=auth, verify=settings.verify, timeout=_KEYSTONE_ADMIN_TIMEOUT_SECONDS)
    return ks_client.Client(session=session)


def _identity_collection(ks, path: str, key: str) -> list[dict]:
    """Reject partial native envelopes before the SDK manager drops their links."""
    payload = ks.get(path)[1]
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get(key), list)
        or not isinstance(payload.get("links", {}), dict)
        or payload.get("links", {}).get("next")
        or payload.get("truncated") not in (None, False)
        or any(not isinstance(row, dict) for row in payload[key])
    ):
        raise ValueError("Incomplete Keystone identity collection")
    return payload[key]


def _load_role_graph(ks) -> tuple[dict[str, set[str]], dict[str, str]]:
    """Bind exact unique global IDs and validate the current provider DAG."""
    catalog = {}
    global_ids = {}
    folded_global_names = set()
    builtin_names = SERVICE_CAPABILITIES | {
        "admin", "manager", "member", "reader",
        "lumen_reader", "lumen_user", "lumen_editor", "lumen_admin",
    }
    seen_builtin_names = set()
    for role in _identity_collection(ks, "/roles", "roles"):
        rid, name, domain = role.get("id"), role.get("name"), role.get("domain_id")
        if (not isinstance(rid, str) or not rid or rid in catalog
                or not isinstance(name, str) or not name
                or (domain is not None and not isinstance(domain, str))):
            raise ValueError("Malformed Keystone role catalog")
        catalog[rid] = role
        folded = name.casefold()
        if folded in builtin_names:
            if folded in seen_builtin_names:
                raise ValueError("Builtin Keystone role name has a domain/global collision")
            seen_builtin_names.add(folded)
        if domain is None:
            # oslo.policy RoleCheck is case-insensitive; two distinct native
            # IDs with casefold-equivalent names must never share authority.
            if folded in folded_global_names:
                raise ValueError("Ambiguous casefold-equivalent global Keystone role names")
            folded_global_names.add(folded)
            global_ids[name] = rid
    graph = {rid: set() for rid in catalog}
    for rule in _identity_collection(ks, "/role_inferences", "role_inferences"):
        prior = rule["prior_role"]["id"]
        children = rule["implies"]
        if prior not in catalog or not isinstance(children, list):
            raise ValueError("Malformed Keystone role inference graph")
        for child in children:
            implied = child["id"]
            if implied not in catalog:
                raise ValueError("Unknown implied Keystone role ID")
            graph[prior].add(implied)
    incoming = dict.fromkeys(graph, 0)
    for children in graph.values():
        for child in children:
            incoming[child] += 1
    pending = [rid for rid, count in incoming.items() if not count]
    visited = 0
    while pending:
        rid = pending.pop()
        visited += 1
        for child in graph[rid]:
            incoming[child] -= 1
            if not incoming[child]:
                pending.append(child)
    if visited != len(graph):
        raise ValueError("Cyclic Keystone role inference graph")
    return graph, global_ids


def _expand_role_ids(role_ids: set[str], graph: dict[str, set[str]]) -> set[str]:
    reached = set()
    pending = list(role_ids)
    while pending:
        rid = pending.pop()
        if rid not in graph:
            raise ValueError("Assigned Keystone role ID absent from current catalog")
        if rid not in reached:
            reached.add(rid)
            pending.extend(graph[rid])
    return reached


def _system_admin_from_graph(ks, user_id: str, graph: dict[str, set[str]], global_ids: dict[str, str]) -> bool:
    """Expand exact raw system grants: effective listings omit system assignments."""
    admin_id = global_ids.get("admin")
    if not user_id or admin_id is None:
        return False
    assignments = [(row, "user", user_id) for row in _identity_collection(
        ks, "/role_assignments?" + urlencode({"user.id": user_id, "scope.system": "all"}), "role_assignments",
    )]
    group_ids = set()
    for group in _identity_collection(ks, f"/users/{quote(user_id, safe='')}/groups", "groups"):
        group_id = group.get("id")
        if not isinstance(group_id, str) or not group_id:
            raise ValueError("Current Keystone group identity unavailable")
        group_ids.add(group_id)
    for group_id in sorted(group_ids):
        assignments.extend((row, "group", group_id) for row in _identity_collection(
            ks, "/role_assignments?" + urlencode({"group.id": group_id, "scope.system": "all"}), "role_assignments",
        ))
    role_ids = set()
    for data, subject_type, subject_id in assignments:
        scope = data.get("scope")
        subject = data.get(subject_type)
        other_subject = "group" if subject_type == "user" else "user"
        if (not isinstance(scope, dict) or set(scope) != {"system"}
                or scope["system"] != {"all": True}
                or not isinstance(scope["system"], dict) or scope["system"].get("all") is not True
                or not isinstance(subject, dict) or subject.get("id") != subject_id
                or data.get(other_subject) is not None):
            continue
        role = data.get("role")
        if not isinstance(role, dict) or not isinstance(role.get("id"), str) or not role["id"]:
            raise ValueError("Current Keystone system role identity unavailable")
        role_ids.add(role["id"])
    return admin_id in _expand_role_ids(role_ids, graph)


def _is_system_admin(user_id: str) -> bool:
    try:
        ks = _get_admin_ks_client()
        graph, global_ids = _load_role_graph(ks)
        return _system_admin_from_graph(ks, user_id, graph, global_ids)
    except Exception as exc:
        _logger.warning("Keystone system admin check failed for user_id=%s", user_id, exc_info=True)
        raise HTTPException(status_code=503, detail="Current Keystone authority unavailable") from exc


def _personal_project_owner(ks, user_id, project_id, project, domains, graph, global_ids, assignments):
    """Native personal provenance only; never infer ownership from member alone."""
    if getattr(project, "afterglow_personal_user_id", None) != user_id:
        return False
    domain = domains[project.domain_id]
    if (getattr(domain, "name", None) != "Personal"
            or getattr(domain, "afterglow_domain_kind", "personal") != "personal"):
        return False
    member = global_ids.get("member")
    reader = global_ids.get("reader")
    if member is None or not _expand_role_ids({member}, graph) <= {member, reader}:
        return False
    raw = _identity_collection(ks, "/role_assignments?" + urlencode({"scope.project.id": project_id}), "role_assignments")

    def direct(row, permitted):
        scope = row.get("scope")
        links = row.get("links", {})
        return (isinstance(scope, dict) and set(scope) == {"project"}
                and scope["project"].get("id") == project_id
                and row.get("user", {}).get("id") == user_id and "group" not in row
                and row.get("role", {}).get("id") in permitted
                and isinstance(links, dict) and "membership" not in links
                and "OS-INHERIT:inherited_to" not in row
                and (not links.get("assignment") or links["assignment"].rstrip("/").endswith(
                    f"/projects/{project_id}/users/{user_id}/roles/{member}")))

    return bool(raw) and all(direct(row, {member}) for row in raw) and bool(assignments) and all(
        direct(row, {member, reader}) for row in assignments
    ) and any(row.get("role", {}).get("id") == member for row in assignments)


def _resolve_project_authority(
    user_id: str, project_id: str, *, issued_role_ids: tuple[str, ...] | None = None,
) -> dict:
    """Bind fresh native identity/domain state and exact current role IDs.

    Human system authority is an alternative to target membership. Reduced
    credentials instead intersect current roles with their original issued IDs.
    """
    if not user_id or not project_id:
        raise HTTPException(status_code=403, detail="Current project membership required")
    try:
        from keystoneclient.exceptions import NotFound

        ks = _get_admin_ks_client()
        try:
            user = ks.users.get(user_id)
            project = ks.projects.get(project_id)
            if user.id != user_id or project.id != project_id:
                raise ValueError("Current Keystone identity mismatch")
            domains = {}
            for subject in (user, project):
                domain_id = getattr(subject, "domain_id", None)
                if not isinstance(domain_id, str) or not domain_id:
                    raise ValueError("Current Keystone native domain identity unavailable")
                if domain_id not in domains:
                    domain = ks.domains.get(domain_id)
                    if domain.id != domain_id:
                        raise ValueError("Current Keystone domain identity mismatch")
                    domains[domain_id] = domain
            statuses = [getattr(user, "enabled", None), getattr(project, "enabled", None),
                        *(getattr(domain, "enabled", None) for domain in domains.values())]
            if any(not isinstance(status, bool) for status in statuses):
                raise ValueError("Current Keystone owner/project/domain status unavailable")
            if not all(statuses):
                raise HTTPException(status_code=403, detail="Current Keystone owner, project or domain is disabled")
        except NotFound as exc:
            raise HTTPException(status_code=403, detail="Current Keystone owner, project or domain no longer exists") from exc
        graph, global_ids = _load_role_graph(ks)
        assignments = _identity_collection(
            ks, "/role_assignments?" + urlencode({
                "user.id": user_id, "scope.project.id": project_id, "effective": True,
            }), "role_assignments",
        )
        role_ids = set()
        for data in assignments:
            scope, subject = data.get("scope", {}), data.get("user", {})
            if (scope.get("project", {}).get("id") != project_id
                    or subject.get("id") != user_id):
                continue
            rid = data.get("role", {}).get("id")
            if not isinstance(rid, str) or not rid:
                raise ValueError("Current Keystone project role identity unavailable")
            role_ids.add(rid)
        is_system_admin = issued_role_ids is None and _system_admin_from_graph(ks, user_id, graph, global_ids)
        if not role_ids and not is_system_admin:
            raise HTTPException(status_code=403, detail="Current project membership required")
        # Effective native assignments are the authority. Do not fabricate service
        # leaves by walking the Keystone graph: grade permission is local policy.
        if not role_ids.issubset(graph):
            raise ValueError("Assigned Keystone role ID absent from current catalog")
        reached = role_ids if issued_role_ids is None else role_ids.intersection(issued_role_ids)
        roles = sorted(name for name, rid in global_ids.items() if rid in reached)
        result = {"roles": roles, "is_system_admin": is_system_admin}
        if issued_role_ids is None and _personal_project_owner(
            ks, user_id, project_id, project, domains, graph, global_ids, assignments,
        ):
            result["personal_project_owner"] = True
        return result
    except HTTPException:
        raise
    except Exception as exc:
        _logger.warning("Current Keystone authority lookup failed", exc_info=True)
        raise HTTPException(status_code=503, detail="Current Keystone authority unavailable") from exc


def _credential_role_ids(credential_authority: dict, user_id: str, project_id: str) -> tuple[str, ...]:
    """Validate the non-secret original binding before a fresh directory lookup."""
    if (not isinstance(credential_authority, dict)
            or set(credential_authority) != {
                "credential_restricted", "user_id", "source_project_id", "source_system_scope", "issued_role_ids",
            }
            or credential_authority.get("credential_restricted") is not True
            or not isinstance(credential_authority.get("user_id"), str) or not credential_authority["user_id"]
            or not isinstance(credential_authority.get("source_project_id"), str)
            or not isinstance(credential_authority.get("source_system_scope"), str)
            or credential_authority.get("source_system_scope") not in {"", "all"}
            or not isinstance(credential_authority.get("issued_role_ids"), (list, tuple))
            or any(not isinstance(rid, str) or not rid for rid in credential_authority["issued_role_ids"])):
        raise HTTPException(status_code=503, detail="Original credential authority metadata unavailable")
    if (credential_authority["user_id"] != user_id
            or credential_authority["source_project_id"] != project_id
            or credential_authority["source_system_scope"]):
        raise HTTPException(status_code=403, detail="Reduced credentials require their original user/project scope")
    return tuple(credential_authority["issued_role_ids"])


def credential_authority_snapshot(info: dict) -> dict | None:
    """Freeze only a reduced caller's identity/source/ID ceiling, never its bearer."""
    if info.get("credential_restricted", False) is False:
        return None
    snapshot = {
        "credential_restricted": info.get("credential_restricted"),
        "user_id": info.get("user_id"),
        "source_project_id": info.get("source_project_id"),
        "source_system_scope": info.get("source_system_scope"),
        "issued_role_ids": info.get("issued_role_ids"),
    }
    role_ids = _credential_role_ids(snapshot, info.get("user_id", ""), info.get("project_id", ""))
    snapshot["issued_role_ids"] = list(role_ids)
    return snapshot


async def resolve_project_authority(
    user_id: str, project_id: str, *, credential_authority: dict | None = None,
) -> dict:
    if credential_authority is None:
        return await run_in_threadpool(_resolve_project_authority, user_id, project_id)
    issued_role_ids = _credential_role_ids(credential_authority, user_id, project_id)
    return await run_in_threadpool(_resolve_project_authority, user_id, project_id, issued_role_ids=issued_role_ids)


def validate_token(token: str, project_id: str = "", target_project_id: str = "") -> dict:
    """Inspect the submitted token before any exchange can erase its attenuation."""
    from keystoneauth1 import access as ks_access
    from keystoneauth1 import session as ks_session
    from keystoneauth1.identity import v3
    from keystoneclient.exceptions import Forbidden, NotFound, Unauthorized

    settings = get_settings()
    try:
        body = _get_admin_ks_client().tokens.get_token_data(token, include_catalog=False)
        original = ks_access.create(body=body, auth_token=token)
    except (Forbidden, NotFound, Unauthorized) as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired original Keystone token") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Original Keystone token validation unavailable") from exc
    try:
        data = body["token"]
        methods = data.get("methods") or []
        credential = data.get("application_credential") or {}
        if (not isinstance(methods, list) or any(not isinstance(method, str) for method in methods)
                or not isinstance(credential, dict)):
            raise ValueError("Malformed original credential metadata")
        if data.get("bind") or data.get("access_rules") or credential.get("access_rules"):
            raise HTTPException(status_code=403, detail="Bound and access-rule credentials are not supported")
        restricted = (
            not methods
            or bool(set(methods) - {"password", "token", "totp", "mapped", "external", "oidc", "saml2"})
            or original.trust_scoped
            or any(key in data for key in (
                "OS-TRUST:trust", "application_credential", "OS-OAUTH1", "OS-OAUTH1:access_token",
                "OS-OAUTH2", "OS-OAUTH2:access_token", "oauth1", "oauth2", "oauth2_credential",
            ))
        )
        u_id = original.user_id
        if not isinstance(u_id, str) or not u_id:
            raise ValueError("Original Keystone caller identity unavailable")
        source_project = original.project_id or ""
        source_system = "all" if original.system_scoped else ""
        if "project" in data and (
            not isinstance(data["project"], dict) or not isinstance(source_project, str) or not source_project
        ):
            raise ValueError("Malformed original Keystone project scope")
        if "system" in data and (
            source_project or data["system"] != {"all": True} or not source_system
            or data["system"].get("all") is not True
        ):
            raise ValueError("Malformed original Keystone system scope")
        issued_roles = data.get("roles", [])
        if not isinstance(issued_roles, list) or any(
            not isinstance(role, dict) or not isinstance(role.get("id"), str) or not role["id"]
            for role in issued_roles
        ):
            raise ValueError("Original Keystone role IDs unavailable")
        issued_role_ids = tuple(sorted({role["id"] for role in issued_roles}))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Original Keystone token metadata unavailable") from exc

    requested_project = project_id.strip() if isinstance(project_id, str) else ""
    target_p = target_project_id.strip() if isinstance(target_project_id, str) else ""
    auth_ref = original
    if requested_project and requested_project != source_project:
        if restricted:
            raise HTTPException(status_code=403, detail="Reduced credentials cannot change their original project scope")
        plugin = v3.Token(auth_url=settings.keystone_auth_url, token=token, project_id=requested_project)
        sess = ks_session.Session(auth=plugin, verify=settings.verify, timeout=_KEYSTONE_ADMIN_TIMEOUT_SECONDS)
        try:
            auth_ref = plugin.get_access(sess)
        except Exception as exc:
            raise HTTPException(status_code=401, detail="Invalid Keystone token project rescope") from exc
        finally:
            sess.session.close()
        if auth_ref.user_id != u_id or auth_ref.project_id != requested_project:
            raise HTTPException(status_code=403, detail="Keystone rescope changed the original identity or requested scope")

    conn_p_id = auth_ref.project_id or ""
    if not conn_p_id and not target_p:
        raise HTTPException(status_code=401, detail="Project-scoped Keystone token or explicit admin target required")
    is_sys_admin = not restricted and _is_system_admin(u_id)
    if target_p and target_p != conn_p_id and not is_sys_admin:
        raise HTTPException(status_code=403, detail="Target project override requires system admin privileges")
    return {
        "user_id": u_id,
        "username": original.username or "",
        "project_id": target_p or conn_p_id,
        "connection_project_id": conn_p_id,
        "connection_system_scope": "all" if auth_ref.system_scoped else "",
        "source_project_id": source_project,
        "source_system_scope": source_system,
        "credential_restricted": restricted,
        "issued_role_ids": issued_role_ids,
        "roles": list(original.role_names or []),
        "is_system_admin": is_sys_admin,
        "auth_token": auth_ref.auth_token or token,
    }


def _keystone_principal(info: dict, token: str) -> Principal:
    return {
        "auth_type": "keystone",
        "user_id": info["user_id"],
        "project_id": info["project_id"],
        "connection_project_id": info.get("connection_project_id", info["project_id"]),
        "connection_system_scope": info.get("connection_system_scope", ""),
        "source_project_id": info.get("source_project_id", info.get("connection_project_id", info["project_id"])),
        "source_system_scope": info.get("source_system_scope", ""),
        "credential_restricted": info.get("credential_restricted", False),
        "issued_role_ids": info.get("issued_role_ids", ()),
        "api_key_id": None,
        "scopes": (),
        "source": "web",
        "roles": list(info["roles"]),
        "is_system_admin": bool(info["is_system_admin"]),
        "token": info.get("auth_token") or token,
    }


async def _refresh_token_authority(info: dict) -> None:
    """Bind current target authority without rescoping the caller connection.

    Token validation's earlier admin lookup cannot authorize a foreign target
    after the final lookup observes revocation, even if project leaves remain.
    """
    if info.get("credential_restricted"):
        if info["project_id"] != info["source_project_id"]:
            raise HTTPException(status_code=403, detail="Reduced credentials require their original project")
        authority = await run_in_threadpool(
            _resolve_project_authority, info["user_id"], info["project_id"], issued_role_ids=info["issued_role_ids"],
        )
    else:
        authority = await resolve_project_authority(info["user_id"], info["project_id"])
    connection_project_id = info.get("connection_project_id", info["project_id"])
    if info["project_id"] != connection_project_id and not authority["is_system_admin"]:
        raise HTTPException(status_code=403, detail="Target project override requires system admin privileges")
    # A logical admin target or a rescope is not the original personal credential.
    if (info.get("source_project_id", connection_project_id) != info["project_id"]
            or info.get("source_system_scope") or info.get("credential_restricted")):
        authority.pop("personal_project_owner", None)
    info.pop("personal_project_owner", None)
    info.update(authority)


async def get_principal(
    request: Request,
    _keystone_token: str | None = Security(keystone_token_scheme),
    _keystone_bearer: HTTPAuthorizationCredentials | None = Security(keystone_bearer_scheme),
    _api_key_bearer: HTTPAuthorizationCredentials | None = Security(api_key_bearer_scheme),
    _x_api_key: str | None = Security(x_api_key_scheme),
) -> Principal:
    """Resolve exactly one Keystone or scoped API-key credential for user routes."""
    from lumen.services import api_key_store as aks

    del _keystone_token, _keystone_bearer, _api_key_bearer, _x_api_key

    x_api_key = (request.headers.get("X-API-Key") or "").strip()
    authorization = (request.headers.get("Authorization") or "").strip()
    x_auth_token = (request.headers.get("X-Auth-Token") or "").strip()
    if x_api_key and authorization:
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=400, detail="인증 credential 조합이 올바르지 않습니다")
        bearer_value = authorization[7:].strip()
        if not bearer_value or not hmac.compare_digest(x_api_key, bearer_value):
            raise HTTPException(status_code=400, detail="API 키 인증 credential이 일치하지 않습니다")
        authorization = ""

    supplied = [value for value in (x_api_key, authorization, x_auth_token) if value]
    if len(supplied) > 1:
        raise HTTPException(status_code=400, detail="여러 인증 credential을 동시에 보낼 수 없습니다")

    if not supplied:
        raise HTTPException(status_code=401, detail="인증 credential이 필요합니다")

    raw_api_key: str | None = None
    keystone_token: str | None = None
    if x_api_key:
        raw_api_key = x_api_key
    elif x_auth_token:
        keystone_token = x_auth_token
    elif authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
        if bearer.startswith("sk-afgl-"):
            raw_api_key = bearer
        else:
            keystone_token = bearer
    else:
        raise HTTPException(status_code=401, detail="유효한 Bearer 인증 credential이 필요합니다")

    if raw_api_key is not None:
        info = await aks.verify_key(raw_api_key)
        if info is None:
            raise HTTPException(status_code=401, detail="유효하지 않은 API 키입니다")
        requested_project = (request.headers.get("X-Project-Id") or "").strip()
        if requested_project and requested_project != info["project_id"]:
            raise HTTPException(status_code=403, detail="API 키 프로젝트와 X-Project-Id가 일치하지 않습니다")
        requested_target = (request.headers.get("X-Target-Project-Id") or "").strip()
        if requested_target and requested_target != info["project_id"]:
            raise HTTPException(status_code=403, detail="API 키 프로젝트와 X-Target-Project-Id가 일치하지 않습니다")
        scopes = info.get("scopes")
        if not isinstance(scopes, tuple):
            raise HTTPException(status_code=401, detail="유효하지 않은 API 키입니다")
        principal: Principal = {
            "auth_type": "api_key",
            "user_id": info["user_id"],
            "project_id": info["project_id"],
            "connection_project_id": info["project_id"],
            "api_key_id": info["api_key_id"],
            "scopes": scopes,
            "source": "api",
            "roles": list(info.get("roles", ())),
            "credential_kind": info.get("credential_kind", "api_key"),
            "expires_at": info.get("expires_at"),
            "is_system_admin": False,
            "service_system_admin": bool(info.get("service_system_admin", False)),
            "personal_project_owner": info.get("personal_project_owner") is True,
        }
    else:
        assert keystone_token is not None
        x_proj = (request.headers.get("X-Project-Id") or "").strip()
        x_target_proj = (request.headers.get("X-Target-Project-Id") or "").strip()
        try:
            val_kwargs = {"project_id": x_proj}
            if x_target_proj:
                val_kwargs["target_project_id"] = x_target_proj
            principal = _keystone_principal(
                await run_in_threadpool(validate_token, keystone_token, **val_kwargs),
                keystone_token,
            )
            await _refresh_token_authority(principal)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=401, detail="인증 실패") from exc

    request.state.token_info = principal
    return principal


def ensure_scopes(principal: Principal, *required: str) -> Principal:
    """Require current service authority AND, for a key, its attenuating scopes."""
    from lumen.policy import authorize

    missing = sorted(action for action in set(required) if not authorize(action, principal))
    if missing:
        raise HTTPException(status_code=403, detail=f"Required service action denied: {', '.join(missing)}")
    return principal


def require_scopes(*required: str):
    async def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        return ensure_scopes(principal, *required)

    return dependency


def require_api_key_scopes(*required: str):
    scoped_dependency = require_scopes(*required)

    async def dependency(principal: Principal = Depends(scoped_dependency)) -> Principal:
        if principal["auth_type"] != "api_key":
            raise HTTPException(status_code=401, detail="API 키가 필요합니다")
        return principal

    return dependency


async def require_token(
    x_auth_token: str | None = Security(keystone_token_scheme),
    bearer: HTTPAuthorizationCredentials | None = Security(keystone_bearer_scheme),
    x_project_id: str | None = Header(None, alias="X-Project-Id"),
    x_target_project_id: str | None = Header(None, alias="X-Target-Project-Id"),
) -> dict:
    token = x_auth_token
    if not token and bearer and bearer.credentials:
        token = bearer.credentials.strip()
    if not token:
        raise HTTPException(status_code=401, detail="X-Auth-Token 또는 Bearer 토큰이 필요합니다")

    proj_id = x_project_id if isinstance(x_project_id, str) else ""
    target_proj_id = x_target_project_id if isinstance(x_target_project_id, str) else ""

    try:
        val_kwargs = {"project_id": proj_id}
        if target_proj_id.strip():
            val_kwargs["target_project_id"] = target_proj_id
        info = await run_in_threadpool(validate_token, token, **val_kwargs)
        info["token"] = info.get("auth_token") or token
        info.setdefault("connection_project_id", info.get("project_id", ""))
        await _refresh_token_authority(info)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="인증 실패")
    return info


def get_token_info(token_info: dict = Depends(require_token)) -> dict:
    return token_info


def require_admin(token_info: dict = Depends(require_token)) -> dict:
    if not token_info.get("is_system_admin"):
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다")
    from lumen.policy import authorize

    if not authorize("system:admin", token_info):
        raise HTTPException(status_code=403, detail="System administration denied by policy")
    return token_info


def require_chat_api_host(request: Request) -> None:
    settings = get_settings()
    hosts_str = settings.chat_api_hosts.strip()
    if not hosts_str:
        return
    allowed = [h.strip().lower() for h in hosts_str.split(",") if h.strip()]
    if not allowed:
        return
    host = (request.headers.get("host") or "").split(":")[0].strip().lower()
    if host not in allowed:
        raise HTTPException(status_code=404, detail="Not Found")


async def get_os_conn(token_info: dict = Depends(require_token)):
    import openstack

    settings = get_settings()
    token = token_info.get("token") or ""
    project_id = token_info.get("connection_project_id", token_info.get("project_id", ""))
    system_scope = token_info.get("connection_system_scope") or None

    if not token or (not project_id and not token_info.get("is_system_admin")):
        raise HTTPException(status_code=401, detail="OpenStack connection requires a valid caller credential")

    conn = openstack.connect(
        load_yaml_config=False,
        load_envvars=False,
        auth_url=settings.keystone_auth_url,
        auth_type="token",
        token=token,
        project_id=project_id or None,
        system_scope=system_scope,
        project_domain_name=settings.keystone_domain,
        region_name=settings.keystone_region_name,
        interface=settings.keystone_interface,
        verify=settings.verify,
    )

    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass
