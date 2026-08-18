"""Keystone token validation and service-scoped OpenStack connections for Lumen."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict

from fastapi import Depends, Header, HTTPException, Request, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from lumen.config import get_settings

keystone_token_scheme = APIKeyHeader(name="X-Auth-Token", auto_error=False, scheme_name="KeystoneToken")
keystone_bearer_scheme = HTTPBearer(auto_error=False, scheme_name="KeystoneBearer")

api_key_bearer_scheme = HTTPBearer(auto_error=False, scheme_name="APIKeyBearer")
x_api_key_scheme = APIKeyHeader(name="x-api-key", auto_error=False, scheme_name="XApiKey")

_logger = logging.getLogger(__name__)
_admin_role_id_cache: str | None = None


class Principal(TypedDict):
    auth_type: Literal["keystone", "api_key"]
    user_id: str
    project_id: str
    api_key_id: int | None
    scopes: tuple[str, ...]
    source: Literal["web", "api"]
    roles: list[str]
    is_system_admin: bool
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
    session = ks_session.Session(auth=auth, verify=settings.verify)
    return ks_client.Client(session=session)


def _resolve_admin_role_id() -> str | None:
    global _admin_role_id_cache
    if _admin_role_id_cache is not None:
        return _admin_role_id_cache
    try:
        ks = _get_admin_ks_client()
        roles = ks.roles.list(name="admin")
        if roles:
            _admin_role_id_cache = roles[0].id
            return _admin_role_id_cache
    except Exception:
        _logger.warning("Failed to resolve admin role ID from Keystone", exc_info=True)
    return None


def _is_system_admin(user_id: str) -> bool:
    try:
        ks = _get_admin_ks_client()
        admin_role_id = _resolve_admin_role_id()
        if not admin_role_id:
            return False
        assignments = ks.role_assignments.list(user=user_id, role=admin_role_id)
        for a in assignments:
            scope = getattr(a, "scope", {}) or {}
            if "system" in scope and scope["system"].get("all") is True:
                return True
            if "domain" in scope:
                return True
        return False
    except Exception:
        _logger.warning("Keystone system admin check failed for user_id=%s", user_id, exc_info=True)
        return False


def validate_token(token: str, project_id: str = "") -> dict:
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
        tok_data = auth.get_token_data(sess)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="유효하지 않거나 만료된 Keystone 토큰입니다") from exc

    tok = tok_data.get("token", {})
    user = tok.get("user") or {}
    proj = tok.get("project") or {}
    roles = [r.get("name", "") for r in tok.get("roles", [])]

    u_id = user.get("id", "")
    p_id = proj.get("id", "")
    if not p_id:
        raise HTTPException(status_code=401, detail="Project-scoped Keystone token required")
    is_sys_admin = "admin" in roles and _is_system_admin(u_id)
    return {
        "user_id": u_id,
        "username": user.get("name", ""),
        "project_id": p_id,
        "roles": roles,
        "is_system_admin": is_sys_admin,
    }


def _keystone_principal(info: dict, token: str) -> Principal:
    return {
        "auth_type": "keystone",
        "user_id": info["user_id"],
        "project_id": info["project_id"],
        "api_key_id": None,
        "scopes": (),
        "source": "web",
        "roles": list(info["roles"]),
        "is_system_admin": bool(info["is_system_admin"]),
        "token": token,
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
        scopes = info.get("scopes")
        if not isinstance(scopes, tuple) or not scopes:
            raise HTTPException(status_code=401, detail="유효하지 않은 API 키입니다")
        principal: Principal = {
            "auth_type": "api_key",
            "user_id": info["user_id"],
            "project_id": info["project_id"],
            "api_key_id": info["api_key_id"],
            "scopes": scopes,
            "source": "api",
            "roles": [],
            "is_system_admin": False,
        }
    else:
        assert keystone_token is not None
        try:
            principal = _keystone_principal(
                validate_token(keystone_token, project_id=request.headers.get("X-Project-Id") or ""),
                keystone_token,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=401, detail="인증 실패") from exc

    request.state.token_info = principal
    return principal


def ensure_scopes(principal: Principal, *required: str) -> Principal:
    """Enforce every required scope for API keys; Keystone sessions retain existing authority."""
    if principal["auth_type"] == "keystone":
        return principal
    missing = tuple(scope for scope in sorted(set(required)) if scope not in principal["scopes"])
    if missing:
        raise HTTPException(status_code=403, detail=f"API 키에 필요한 scope가 없습니다: {', '.join(missing)}")
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
) -> dict:
    token = x_auth_token
    if not token and bearer and bearer.credentials:
        token = bearer.credentials.strip()
    if not token:
        raise HTTPException(status_code=401, detail="X-Auth-Token 또는 Bearer 토큰이 필요합니다")
    try:
        info = validate_token(token, project_id=x_project_id or "")
        info["token"] = token
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


async def get_api_key_info(
    request: Request,
    bearer: HTTPAuthorizationCredentials | None = Security(api_key_bearer_scheme),
    x_api_key: str | None = Security(x_api_key_scheme),
) -> Principal:
    """Legacy API-key-only dependency retained for compat route callers."""
    from lumen.services import api_key_store as aks

    raw = None
    if bearer and bearer.credentials:
        raw = bearer.credentials.strip()
    elif x_api_key:
        raw = x_api_key.strip()
    if not raw:
        raise HTTPException(status_code=401, detail="API 키가 필요합니다 (Authorization: Bearer 또는 x-api-key)")

    info = await aks.verify_key(raw)
    scopes = info.get("scopes") if info else None
    if not isinstance(scopes, tuple) or not scopes:
        raise HTTPException(status_code=401, detail="유효하지 않은 API 키입니다")
    principal: Principal = {
        "auth_type": "api_key",
        "user_id": info["user_id"],
        "project_id": info["project_id"],
        "api_key_id": info["api_key_id"],
        "scopes": scopes,
        "source": "api",
        "roles": [],
        "is_system_admin": False,
    }
    request.state.token_info = principal
    return principal


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
    project_id = token_info.get("project_id") or ""

    if not token or not project_id:
        raise HTTPException(status_code=401, detail="OpenStack connection requires a valid user token and project")

    conn = openstack.connect(
        auth_url=settings.keystone_auth_url,
        auth_type="token",
        token=token,
        project_id=project_id,
        user_domain_name=settings.keystone_domain,
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
