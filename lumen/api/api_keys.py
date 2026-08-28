"""빌트인 AI 채팅 외부 API 키 관리 (사용자 본인).

발급 시에만 평문 키를 1회 반환한다. 이후에는 목록(prefix 마스킹)·폐기만 가능하다.
소유권은 api_key_store 가 강제하고, 예외를 HTTP 상태로 매핑한다.
"""

from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from lumen.auth import get_token_info, require_admin
from lumen.services import api_key_store as aks

router = APIRouter()


ApiKeyScope = Literal[
    "models:read",
    "compat:completions:write",
    "native:conversations:read",
    "native:conversations:write",
    "native:runs:read",
    "native:runs:write",
    "native:extensions:read",
    "native:extensions:write",
    "native:tools:execute",
    "native:memory:read",
    "native:memory:write",
    "native:agents:use",
    "usage:read",
]


class ApiKeyCreateBody(BaseModel):
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

def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, aks.ApiKeyLimitConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, aks.ApiKeyNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, aks.ApiKeyForbidden):
        return HTTPException(status_code=403, detail=str(exc))
    return HTTPException(status_code=503, detail=str(exc))


_EXC = (aks.ApiKeyLimitConflict, aks.ApiKeyNotFound, aks.ApiKeyForbidden, aks.ApiKeyStorageUnavailable)

@router.get("/api-keys")
async def list_api_keys(token_info: dict = Depends(get_token_info)):
    # 선택적 목록 — 저장소 미가용 시 빈 목록으로 degrade(핵심 채팅과 동일 정책).
    try:
        return await aks.list_keys(token_info["user_id"], token_info["project_id"])
    except aks.ApiKeyStorageUnavailable:
        return []


@router.post("/api-keys", status_code=201)
async def create_api_key(body: ApiKeyCreateBody, token_info: dict = Depends(get_token_info)):
    """새 API 키 발급 — 응답의 `key` 는 평문(1회만 노출, 이후 조회 불가)."""
    try:
        return await aks.create_key(
            token_info["user_id"],
            token_info["project_id"],
            body.name or "",
            list(body.scopes),
            body.monthly_credit_limit,
        )
    except _EXC as exc:
        raise _http(exc) from exc


@router.delete("/api-keys/{key_id}", status_code=204)
async def revoke_api_key(key_id: int, token_info: dict = Depends(get_token_info)):
    try:
        await aks.revoke_key(key_id, token_info["user_id"], token_info["project_id"])
    except _EXC as exc:
        raise _http(exc) from exc


@router.patch("/api-keys/{key_id}/limits")
async def update_owner_api_key_limits(
    key_id: int,
    body: ApiKeyLimitUpdateBody,
    token_info: dict = Depends(get_token_info),
):
    try:
        return await aks.update_owner_limits(
            key_id,
            token_info["user_id"],
            token_info["project_id"],
            body.monthly_credit_limit,
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
