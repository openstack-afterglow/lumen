"""Keystone token validation and service-scoped OpenStack connections for Lumen."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request

from lumen.config import get_settings

_logger = logging.getLogger(__name__)
_admin_role_id_cache: str | None = None


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
    from keystoneclient.v3 import client as ks_client
    from keystoneauth1.identity import v3
    from keystoneauth1 import session as ks_session

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
    from keystoneauth1.identity import v3
    from keystoneauth1 import session as ks_session

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
    user = tok.get("user", {})
    proj = tok.get("project", {})
    roles = [r.get("name", "") for r in tok.get("roles", [])]

    u_id = user.get("id", "")
    is_sys_admin = "admin" in roles and _is_system_admin(u_id)

    return {
        "user_id": u_id,
        "username": user.get("name", ""),
        "project_id": proj.get("id", ""),
        "project_name": proj.get("name", ""),
        "roles": roles,
        "is_system_admin": is_sys_admin,
    }


async def require_token(
    x_auth_token: str | None = Header(None, alias="X-Auth-Token"),
    x_project_id: str | None = Header(None, alias="X-Project-Id"),
    authorization: str | None = Header(None, alias="Authorization"),
) -> dict:
    token = x_auth_token
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="X-Auth-Token 헤더가 필요합니다")
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
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None),
) -> dict:
    from lumen.services import api_key_store as aks

    raw = None
    if authorization and authorization.startswith("Bearer "):
        raw = authorization[7:].strip()
    elif x_api_key:
        raw = x_api_key.strip()
    if not raw:
        raise HTTPException(status_code=401, detail="API 키가 필요합니다 (Authorization: Bearer 또는 x-api-key)")

    info = await aks.verify_key(raw)
    if info is None:
        raise HTTPException(status_code=401, detail="유효하지 않은 API 키입니다")

    token_info = {
        "user_id": info["user_id"],
        "project_id": info["project_id"],
        "api_key_id": info["api_key_id"],
        "source": "api",
        "roles": [],
        "is_system_admin": False,
    }
    request.state.token_info = token_info
    return token_info


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
