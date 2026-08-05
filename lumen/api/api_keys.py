"""빌트인 AI 채팅 외부 API 키 관리 (사용자 본인).

발급 시에만 평문 키를 1회 반환한다. 이후에는 목록(prefix 마스킹)·폐기만 가능하다.
소유권은 api_key_store 가 강제하고, 예외를 HTTP 상태로 매핑한다.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from lumen.auth import get_token_info
from lumen.services import api_key_store as aks

router = APIRouter()


class ApiKeyCreateBody(BaseModel):
    name: str | None = Field(default="", max_length=100)


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, aks.ApiKeyNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, aks.ApiKeyForbidden):
        return HTTPException(status_code=403, detail=str(exc))
    return HTTPException(status_code=503, detail=str(exc))


_EXC = (aks.ApiKeyNotFound, aks.ApiKeyForbidden, aks.ApiKeyStorageUnavailable)


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
        return await aks.create_key(token_info["user_id"], token_info["project_id"], body.name or "")
    except _EXC as exc:
        raise _http(exc) from exc


@router.delete("/api-keys/{key_id}", status_code=204)
async def revoke_api_key(key_id: int, token_info: dict = Depends(get_token_info)):
    try:
        await aks.revoke_key(key_id, token_info["user_id"], token_info["project_id"])
    except _EXC as exc:
        raise _http(exc) from exc
