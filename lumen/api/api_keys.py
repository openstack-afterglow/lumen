"""빌트인 AI 채팅 외부 API 키 관리 (사용자 본인).

발급 시에만 평문 키를 1회 반환한다. 이후에는 목록(prefix 마스킹)·폐기만 가능하다.
소유권은 api_key_store 가 강제하고, 예외를 HTTP 상태로 매핑한다.
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lumen import service_authority
from lumen.auth import (
    Principal,
    credential_authority_snapshot,
    ensure_scopes,
    get_principal,
    get_token_info,
    require_admin,
)
from lumen.services import api_key_store as aks
from lumen.services.mcp_domain_authority import resolve_domain_authority

router = APIRouter()


# Keep the public validation/schema vocabulary identical to the service contract.
ApiKeyScope = Literal[tuple(sorted(aks.API_KEY_SCOPES))]


class ApiKeyCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default="", max_length=100)
    scopes: list[ApiKeyScope] = Field(
        default_factory=lambda: list(aks.DEFAULT_API_KEY_SCOPES),
        min_length=1,
        max_length=len(aks.API_KEY_SCOPES),
    )
    monthly_credit_limit: Decimal | None = Field(
        default=None,
        gt=0,
        max_digits=18,
        decimal_places=8,
    )
    weekly_credit_limit: Decimal | None = Field(
        default=None,
        gt=0,
        max_digits=18,
        decimal_places=8,
    )
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def expiry_must_be_aware_and_future(cls, value: datetime | None) -> datetime | None:
        return aks.validate_expires_at(value)

    @field_validator("scopes")
    @classmethod
    def scopes_must_be_unique(cls, scopes: list[ApiKeyScope]) -> list[ApiKeyScope]:
        if len(scopes) != len(set(scopes)):
            raise ValueError("scope는 중복될 수 없습니다")
        return scopes


class ApiKeyLimitUpdateBody(BaseModel):
    monthly_credit_limit: Decimal | None = Field(
        ...,
        gt=0,
        max_digits=18,
        decimal_places=8,
    )


class ApiKeyOwnerLimitUpdateBody(BaseModel):
    monthly_credit_limit: Decimal | None = Field(
        default=None,
        gt=0,
        max_digits=18,
        decimal_places=8,
    )
    weekly_credit_limit: Decimal | None = Field(
        default=None,
        gt=0,
        max_digits=18,
        decimal_places=8,
    )

    @model_validator(mode="after")
    def require_limit(self):
        if not self.model_fields_set:
            raise ValueError("변경할 API 키 한도를 하나 이상 지정해야 합니다")
        return self


class ApiKeyUpdateBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, name: str) -> str:
        if not name.strip():
            raise ValueError("이름은 공백일 수 없습니다")
        return name


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, aks.ApiKeyInvalidExpiry):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, aks.ApiKeyLimitConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, aks.ApiKeyNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, aks.ApiKeyForbidden):
        return HTTPException(status_code=403, detail=str(exc))
    return HTTPException(status_code=503, detail=str(exc))


_EXC = (aks.ApiKeyInvalidExpiry, aks.ApiKeyLimitConflict, aks.ApiKeyNotFound, aks.ApiKeyForbidden, aks.ApiKeyStorageUnavailable)


@router.get("/api-keys")
async def list_api_keys(token_info: dict = Depends(get_token_info)):
    ensure_scopes(token_info, "native:keys:read")
    # 선택적 목록 — 저장소 미가용 시 빈 목록으로 degrade(핵심 채팅과 동일 정책).
    try:
        return await aks.list_keys(token_info["user_id"])
    except aks.ApiKeyStorageUnavailable:
        return []


@router.post("/api-keys", status_code=201)
async def create_api_key(body: ApiKeyCreateBody, token_info: dict = Depends(get_token_info)):
    """새 API 키 발급 — 응답의 `key` 는 평문(1회만 노출, 이후 조회 불가)."""
    ensure_scopes(token_info, "native:keys:write", *body.scopes)
    try:
        return await aks.create_key(
            token_info["user_id"],
            token_info["project_id"],
            body.name or "",
            list(body.scopes),
            body.monthly_credit_limit,
            body.weekly_credit_limit,
            expires_at=body.expires_at,
            credential_authority=credential_authority_snapshot(token_info),
        )
    except _EXC as exc:
        raise _http(exc) from exc


@router.get("/api-keys/issuance-policy")
async def get_api_key_issuance_policy(token_info: dict = Depends(get_token_info)):
    ensure_scopes(token_info, "native:keys:write")
    try:
        permitted = await aks.current_issuance_scopes(
            token_info["user_id"], token_info["project_id"],
            credential_authority=credential_authority_snapshot(token_info),
        )
        permitted &= service_authority.allowed_scopes(token_info)
        if token_info.get("auth_type") == "api_key":
            permitted &= set(token_info.get("scopes", ()))
        allowed = sorted(permitted)
        policy = await aks.owner_issuance_policy(token_info["user_id"])
    except _EXC as exc:
        raise _http(exc) from exc
    return {
        **policy,
        "allowed_scopes": allowed,
        "scope_capabilities": {scope: sorted(service_authority.SCOPE_CAPABILITIES[scope]) for scope in allowed},
    }




@router.patch("/api-keys/{key_id}")
async def update_api_key(
    key_id: int,
    body: ApiKeyUpdateBody,
    token_info: dict = Depends(get_token_info),
):
    ensure_scopes(token_info, "native:keys:write")
    try:
        return await aks.rename_key(
            key_id,
            token_info["user_id"],
            body.name,
        )
    except _EXC as exc:
        raise _http(exc) from exc


@router.delete("/api-keys/{key_id}", status_code=204)
async def revoke_api_key(key_id: int, token_info: dict = Depends(get_token_info)):
    ensure_scopes(token_info, "native:keys:delete")
    try:
        await aks.revoke_key(key_id, token_info["user_id"])
    except _EXC as exc:
        raise _http(exc) from exc


@router.patch("/api-keys/{key_id}/limits")
async def update_owner_api_key_limits(
    key_id: int,
    body: ApiKeyOwnerLimitUpdateBody,
    token_info: dict = Depends(get_token_info),
):
    ensure_scopes(token_info, "native:keys:write")
    try:
        return await aks.update_owner_limits(
            key_id,
            token_info["user_id"],
            body.model_dump(exclude_unset=True),
        )
    except _EXC as exc:
        raise _http(exc) from exc


@router.get("/admin/api-keys")
async def list_admin_api_keys(
    owner_user_id: str | None = Query(default=None),
    owner_project_id: str | None = Query(default=None),
    before_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=50, ge=1, le=200),
    admin_info: dict = Depends(require_admin),
):
    try:
        return await aks.list_keys_admin(
            owner_user_id=owner_user_id,
            owner_project_id=owner_project_id,
            before_id=before_id,
            limit=limit,
        )
    except _EXC as exc:
        raise _http(exc) from exc


@router.patch("/admin/api-keys/{key_id}/limits")
async def update_admin_api_key_limits(
    key_id: int,
    body: ApiKeyLimitUpdateBody,
    admin_info: dict = Depends(require_admin),
):
    try:
        return await aks.update_admin_limits(
            key_id,
            body.monthly_credit_limit,
        )
    except _EXC as exc:
        raise _http(exc) from exc


async def _require_service_key_manager(principal: Principal = Depends(get_principal)) -> Principal:
    if principal["credential_class"] != "keystone" or principal.get("credential_restricted"):
        raise HTTPException(403, "An unrestricted Keystone session is required")
    connection_project_id = principal.get("connection_project_id")
    if not connection_project_id or connection_project_id != principal["project_id"]:
        raise HTTPException(403, "Service keys require the current domain")
    authority = await resolve_domain_authority(
        principal["user_id"], principal["project_id"],
        unrestricted_human=True, compute_manage=True, allow_system_admin=False,
    )
    if not authority["can_manage"] or authority["domain_id"] != principal["domain_id"]:
        raise HTTPException(403, "Current domain manager authority is required")
    return principal


@router.get("/service-keys")
async def list_service_keys(principal: Principal = Depends(_require_service_key_manager)):
    try:
        return await aks.list_service_keys(principal["domain_id"])
    except _EXC as exc:
        raise _http(exc) from exc


@router.post("/service-keys", status_code=201)
async def create_service_key(body: ApiKeyCreateBody, principal: Principal = Depends(_require_service_key_manager)):
    try:
        return await aks.create_service_key(
            principal["domain_id"], principal["user_id"], body.name or "", list(body.scopes),
            body.monthly_credit_limit, body.weekly_credit_limit, expires_at=body.expires_at,
        )
    except _EXC as exc:
        raise _http(exc) from exc


@router.patch("/service-keys/{key_id}/limits")
async def update_service_key_limits(
    key_id: int,
    body: ApiKeyOwnerLimitUpdateBody,
    principal: Principal = Depends(_require_service_key_manager),
):
    try:
        return await aks.update_service_limits(key_id, principal["domain_id"], body.model_dump(exclude_unset=True))
    except _EXC as exc:
        raise _http(exc) from exc


@router.delete("/service-keys/{key_id}", status_code=204)
async def revoke_service_key(key_id: int, principal: Principal = Depends(_require_service_key_manager)):
    try:
        await aks.revoke_service_key(key_id, principal["domain_id"])
    except _EXC as exc:
        raise _http(exc) from exc


@router.get("/admin/service-keys")
async def list_admin_service_keys(
    owner_domain_id: str | None = Query(default=None),
    before_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=50, ge=1, le=200),
    admin_info: dict = Depends(require_admin),
):
    try:
        return await aks.list_keys_admin(kind="service", owner_domain_id=owner_domain_id, before_id=before_id, limit=limit)
    except _EXC as exc:
        raise _http(exc) from exc
