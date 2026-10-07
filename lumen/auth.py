"""Keystone token validation and service-scoped OpenStack connections for Lumen."""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict

from fastapi import Depends, Header, HTTPException, Request, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from starlette.concurrency import run_in_threadpool

from lumen.config import get_settings
from lumen.service_authority import SERVICE_CAPABILITIES, allowed_scopes

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
    api_key_id: int | None
    scopes: tuple[str, ...]
    source: Literal["web", "api"]
    roles: list[str]
    is_system_admin: bool
    service_system_admin: NotRequired[bool]
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


def _load_role_graph(ks) -> tuple[dict[str, set[str]], dict[str, str], dict[str, str]]:
    """Bind exact unique global IDs and validate the current provider DAG."""
    catalog = {}
    global_ids = {}
    restricted_ids = {}
    builtin_names = SERVICE_CAPABILITIES | {
        "admin", "manager", "member", "reader",
        "lumen_reader", "lumen_user", "lumen_editor", "lumen_admin",
    }
    seen_builtin_names = set()
    for role in ks.roles.list():
        rid, name, domain = role.id, role.name, getattr(role, "domain_id", None)
        if (not isinstance(rid, str) or not rid or rid in catalog
                or not isinstance(name, str) or not name
                or (domain is not None and not isinstance(domain, str))):
            raise ValueError("Malformed Keystone role catalog")
        catalog[rid] = role
        if name in builtin_names:
            if name in seen_builtin_names:
                raise ValueError("Builtin Keystone role name has a domain/global collision")
            seen_builtin_names.add(name)
        if name in {"admin", "manager"}:
            restricted_ids[rid] = name
        if domain is None:
            if name in global_ids:
                raise ValueError("Ambiguous global Keystone role name")
            global_ids[name] = rid
    graph = {rid: set() for rid in catalog}
    rules = ks.inference_rules.list_inference_roles()
    if not isinstance(rules, list):
        raise ValueError("Keystone role inference graph unavailable")
    for rule in rules:
        prior = rule.prior_role["id"]
        children = rule.implies
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
    return graph, global_ids, restricted_ids


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
    admin_id = global_ids.get("admin")
    if not user_id or admin_id is None:
        return False
    assignments = ks.role_assignments.list(user=user_id, system="all", effective=True)
    role_ids = {a.role["id"] for a in assignments
                if (getattr(a, "scope", {}) or {}).get("system", {}).get("all") is True}
    return admin_id in _expand_role_ids(role_ids, graph)


def _is_system_admin(user_id: str) -> bool:
    try:
        ks = _get_admin_ks_client()
        graph, global_ids, _restricted_ids = _load_role_graph(ks)
        return _system_admin_from_graph(ks, user_id, graph, global_ids)
    except Exception:
        _logger.warning("Keystone system admin check failed for user_id=%s", user_id, exc_info=True)
        return False


def _resolve_project_authority(user_id: str, project_id: str) -> dict:
    """Read current effective grants, including group and inherited assignments.

    Only a project-scoped effective row establishes membership. Domain and
    project admin labels never stand in for verified system authority.
    """
    if not user_id or not project_id:
        raise HTTPException(status_code=403, detail="Current project membership required")
    try:
        from keystoneclient.exceptions import NotFound

        ks = _get_admin_ks_client()
        try:
            user_enabled = getattr(ks.users.get(user_id), "enabled", None)
            project_enabled = getattr(ks.projects.get(project_id), "enabled", None)
        except NotFound as exc:
            raise HTTPException(status_code=403, detail="Current Keystone owner or project no longer exists") from exc
        if not isinstance(user_enabled, bool) or not isinstance(project_enabled, bool):
            raise ValueError("Current Keystone owner status unavailable")
        if not user_enabled or not project_enabled:
            raise HTTPException(status_code=403, detail="Current Keystone owner or project is disabled")
        graph, global_ids, restricted_ids = _load_role_graph(ks)
        assignments = ks.role_assignments.list(user=user_id, project=project_id, effective=True)
        role_ids = {
            a.role["id"] for a in assignments
            if (getattr(a, "scope", {}) or {}).get("project", {}).get("id") == project_id
        }
        is_system_admin = _system_admin_from_graph(ks, user_id, graph, global_ids)
        if not role_ids and not is_system_admin:
            raise HTTPException(status_code=403, detail="Current project membership required")
        reached = _expand_role_ids(role_ids, graph)
        # Domain aliases never grant builtin authority, but raw native privileged
        # labels still trigger the nonverified-admin fail-closed policy.
        roles = sorted({name for name, rid in global_ids.items() if rid in reached}
                       | {name for rid, name in restricted_ids.items() if rid in reached})
        return {"roles": roles, "is_system_admin": is_system_admin}
    except HTTPException:
        raise
    except Exception as exc:
        _logger.warning("Current Keystone authority lookup failed", exc_info=True)
        raise HTTPException(status_code=503, detail="Current Keystone authority unavailable") from exc


async def resolve_project_authority(user_id: str, project_id: str) -> dict:
    return await run_in_threadpool(_resolve_project_authority, user_id, project_id)


def validate_token(token: str, project_id: str = "", target_project_id: str = "") -> dict:
    settings = get_settings()
    from keystoneauth1 import session as ks_session
    from keystoneauth1.identity import v3

    auth = v3.Token(
        auth_url=settings.keystone_auth_url,
        token=token,
        project_id=project_id or None,
    )
    sess = ks_session.Session(auth=auth, verify=settings.verify)
    try:
        auth_ref = auth.get_access(sess)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail="유효하지 않거나 만료된 Keystone 토큰입니다") from exc

    conn_p_id = auth_ref.project_id or ""
    if not conn_p_id:
        raise HTTPException(status_code=401, detail="Project-scoped Keystone token required")

    u_id = auth_ref.user_id or ""
    roles = list(auth_ref.role_names or [])
    is_sys_admin = _is_system_admin(u_id)
    effective_token = auth_ref.auth_token or token
    if not isinstance(target_project_id, str):
        target_project_id = ""
    target_p = target_project_id.strip()
    if target_p and target_p != conn_p_id:
        if not is_sys_admin:
            raise HTTPException(status_code=403, detail="Target project override requires system admin privileges")
        logical_p_id = target_p
    else:
        logical_p_id = conn_p_id

    return {
        "user_id": u_id,
        "username": auth_ref.username or "",
        "project_id": logical_p_id,
        "connection_project_id": conn_p_id,
        "roles": roles,
        "is_system_admin": is_sys_admin,
        "auth_token": effective_token,
    }


def _keystone_principal(info: dict, token: str) -> Principal:
    return {
        "auth_type": "keystone",
        "user_id": info["user_id"],
        "project_id": info["project_id"],
        "connection_project_id": info.get("connection_project_id") or info["project_id"],
        "api_key_id": None,
        "scopes": (),
        "source": "web",
        "roles": list(info["roles"]),
        "is_system_admin": bool(info["is_system_admin"]),
        "token": info.get("auth_token") or token,
    }


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
        if not isinstance(scopes, tuple) or not scopes:
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
            principal.update(await resolve_project_authority(principal["user_id"], principal["project_id"]))
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=401, detail="인증 실패") from exc

    request.state.token_info = principal
    return principal


def ensure_scopes(principal: Principal, *required: str) -> Principal:
    """Require current service authority AND, for a key, its attenuating scopes."""
    service_admin = principal.get("is_system_admin", False) or principal.get("service_system_admin", False)
    permitted = allowed_scopes(principal.get("roles", ()), service_admin)
    missing = sorted(set(required) - permitted)
    if principal.get("auth_type") == "api_key":
        missing = sorted(set(missing) | (set(required) - set(principal.get("scopes", ()))))
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
        info.update(await resolve_project_authority(info["user_id"], info["project_id"]))
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




def get_admin_connection_for_project(project_id: str):
    import openstack

    settings = get_settings()
    conn = openstack.connect(
        auth_url=settings.keystone_auth_url,
        username=settings.keystone_admin_username,
        password=settings.keystone_admin_password,
        project_name=settings.keystone_admin_project,
        user_domain_name=settings.keystone_domain,
        project_domain_name=settings.keystone_domain,
        region_name=settings.keystone_region_name,
        interface=settings.keystone_interface,
        verify=settings.verify,
    )
    if project_id and conn.current_project_id != project_id:
        conn = conn.connect_to_project(project_id)
    return conn


async def get_os_conn(token_info: dict = Depends(require_token)):
    import openstack

    settings = get_settings()
    token = token_info.get("token") or ""
    project_id = token_info.get("connection_project_id") or token_info.get("project_id") or ""

    if not token or not project_id:
        raise HTTPException(status_code=401, detail="OpenStack connection requires a valid user token and project")

    conn = openstack.connect(
        auth_url=settings.keystone_auth_url,
        auth_type="token",
        token=token,
        project_id=project_id,
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
