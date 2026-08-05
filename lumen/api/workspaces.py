"""빌트인 AI 채팅 프로젝트(workspace) API — 대화 그룹 + 공통 지침. 소유자(user_id) 한정."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from lumen.auth import get_token_info
from lumen.services import workspace_store as ws

router = APIRouter()

_MAX_INSTRUCTIONS = 20000


class WorkspaceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    instructions: str | None = Field(default=None, max_length=_MAX_INSTRUCTIONS)


class WorkspaceUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    instructions: str | None = Field(default=None, max_length=_MAX_INSTRUCTIONS)


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ws.WorkspaceNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ws.WorkspaceForbidden):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ws.WorkspaceValidationError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=503, detail=str(exc))


@router.post("/workspaces", status_code=201)
async def create_workspace(payload: WorkspaceCreate, token_info: dict = Depends(get_token_info)):
    try:
        return await ws.create_workspace(owner_user_id=token_info["user_id"], **payload.model_dump())
    except (ws.WorkspaceValidationError, ws.ChatStorageUnavailable) as exc:
        raise _map_error(exc) from exc


@router.get("/workspaces")
async def list_workspaces(token_info: dict = Depends(get_token_info)):
    # 선택적 기능 목록: 저장소 미가용/데이터 없음은 빈 목록으로 graceful 처리(503 아님).
    try:
        return await ws.list_workspaces(user_id=token_info["user_id"])
    except ws.ChatStorageUnavailable:
        return []


@router.get("/workspaces/{workspace_id}")
async def get_workspace(workspace_id: int, token_info: dict = Depends(get_token_info)):
    try:
        return await ws.get_workspace(workspace_id, user_id=token_info["user_id"])
    except (ws.WorkspaceNotFound, ws.WorkspaceForbidden, ws.ChatStorageUnavailable) as exc:
        raise _map_error(exc) from exc


@router.patch("/workspaces/{workspace_id}")
async def update_workspace(workspace_id: int, payload: WorkspaceUpdate, token_info: dict = Depends(get_token_info)):
    try:
        return await ws.update_workspace(
            workspace_id, user_id=token_info["user_id"], patch=payload.model_dump(exclude_unset=True)
        )
    except (ws.WorkspaceNotFound, ws.WorkspaceForbidden, ws.WorkspaceValidationError, ws.ChatStorageUnavailable) as exc:
        raise _map_error(exc) from exc


@router.delete("/workspaces/{workspace_id}", status_code=204)
async def delete_workspace(workspace_id: int, token_info: dict = Depends(get_token_info)):
    try:
        await ws.delete_workspace(workspace_id, user_id=token_info["user_id"])
    except (ws.WorkspaceNotFound, ws.WorkspaceForbidden, ws.ChatStorageUnavailable) as exc:
        raise _map_error(exc) from exc
