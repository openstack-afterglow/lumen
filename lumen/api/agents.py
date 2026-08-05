"""빌트인 AI 채팅 에이전트 API (사용자·프로젝트 소유 리소스 + 공개 허브).

전 엔드포인트 get_token_info 인증. CRUD는 호출자 user/project에 한정하고,
공개 허브는 복제 가능한 템플릿만 검색한다(IDOR 방어).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from lumen.auth import get_token_info
from lumen.services import agent_store as ags

router = APIRouter()

_MAX_INSTRUCTIONS = 20000


class AgentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    avatar: str | None = Field(default=None, max_length=500)
    instructions: str | None = Field(default=None, max_length=_MAX_INSTRUCTIONS)
    model_name: str | None = Field(default=None, max_length=190)
    params: dict | None = None
    mcp_ids: list[int] | None = None
    tool_ids: list[int] | None = None
    visibility: str = Field(default="private")

    model_config = {"protected_namespaces": ()}


class AgentUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    avatar: str | None = Field(default=None, max_length=500)
    instructions: str | None = Field(default=None, max_length=_MAX_INSTRUCTIONS)
    model_name: str | None = Field(default=None, max_length=190)
    params: dict | None = None
    mcp_ids: list[int] | None = None
    tool_ids: list[int] | None = None
    visibility: str | None = None

    model_config = {"protected_namespaces": ()}


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ags.AgentNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ags.AgentForbidden):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ags.AgentValidationError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=503, detail=str(exc))


@router.post("/agents", status_code=201)
async def create_agent(payload: AgentCreate, token_info: dict = Depends(get_token_info)):
    try:
        return await ags.create_agent(
            owner_user_id=token_info["user_id"],
            project_id=token_info["project_id"],
            **payload.model_dump(),
        )
    except (ags.AgentValidationError, ags.ChatStorageUnavailable) as exc:
        raise _map_error(exc) from exc


@router.get("/agents")
async def list_agents(token_info: dict = Depends(get_token_info)):
    # 선택적 기능 목록: 저장소 미가용/데이터 없음은 빈 목록으로 graceful 처리(503 아님).
    try:
        return await ags.list_agents(user_id=token_info["user_id"], project_id=token_info["project_id"])
    except ags.ChatStorageUnavailable:
        return []


@router.get("/agents/hub")
async def agent_hub(
    query: str = Query(default=""),
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    token_info: dict = Depends(get_token_info),
):
    """공개 에이전트 검색(허브). 이름·설명 부분일치, 인기(clone_count) 순."""
    try:
        return await ags.list_public(
            query=query,
            limit=limit,
            offset=offset,
            user_id=token_info["user_id"],
            project_id=token_info["project_id"],
        )
    except ags.ChatStorageUnavailable:
        return []


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: int, token_info: dict = Depends(get_token_info)):
    try:
        return await ags.get_agent(
            agent_id,
            user_id=token_info["user_id"],
            project_id=token_info["project_id"],
        )
    except (ags.AgentNotFound, ags.AgentForbidden, ags.ChatStorageUnavailable) as exc:
        raise _map_error(exc) from exc


@router.patch("/agents/{agent_id}")
async def update_agent(agent_id: int, payload: AgentUpdate, token_info: dict = Depends(get_token_info)):
    try:
        return await ags.update_agent(
            agent_id,
            user_id=token_info["user_id"],
            project_id=token_info["project_id"],
            patch=payload.model_dump(exclude_unset=True),
        )
    except (ags.AgentNotFound, ags.AgentForbidden, ags.AgentValidationError, ags.ChatStorageUnavailable) as exc:
        raise _map_error(exc) from exc


@router.delete("/agents/{agent_id}", status_code=204)
async def delete_agent(agent_id: int, token_info: dict = Depends(get_token_info)):
    try:
        await ags.delete_agent(
            agent_id,
            user_id=token_info["user_id"],
            project_id=token_info["project_id"],
        )
    except (ags.AgentNotFound, ags.AgentForbidden, ags.ChatStorageUnavailable) as exc:
        raise _map_error(exc) from exc


@router.post("/agents/{agent_id}/clone", status_code=201)
async def clone_agent(agent_id: int, token_info: dict = Depends(get_token_info)):
    """허브의 공개(또는 본인) 에이전트를 내 계정으로 복제(private 사본)."""
    try:
        return await ags.clone_agent(
            agent_id,
            user_id=token_info["user_id"],
            project_id=token_info["project_id"],
        )
    except (ags.AgentNotFound, ags.AgentForbidden, ags.ChatStorageUnavailable) as exc:
        raise _map_error(exc) from exc
