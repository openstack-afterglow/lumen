"""Scoped user memory API.

The request project determines all project/workspace namespaces; clients never
submit an OpenStack project identifier.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from lumen.auth import require_scopes
from lumen.config import get_settings
from lumen.services import memory_retrieval as mr
from lumen.services import memory_store as ms
from lumen.services import workspace_store as ws
from lumen.services.providers import routing as ps
from lumen.services.semantic_memory import SemanticMemoryUnavailable, semantic_memory_available

router = APIRouter()

_MAX_CONTENT = 4000


class MemoryCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=_MAX_CONTENT)
    category: Literal["interest", "development", "habit", "preference", "general"] = "general"
    scope: Literal["account", "project", "workspace"] = "account"
    workspace_id: int | None = Field(default=None, gt=0)


class MemorySearch(BaseModel):
    query: str = Field(..., min_length=1, max_length=_MAX_CONTENT)
    scope: Literal["account", "project", "workspace"]
    workspace_id: int | None = Field(default=None, gt=0)


class MemoryUpdate(BaseModel):
    content: str | None = Field(default=None, max_length=_MAX_CONTENT)
    is_active: bool | None = None
    category: Literal["interest", "development", "habit", "preference", "general"] | None = None


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ms.MemoryNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ms.MemoryForbidden):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ms.MemoryValidationError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=503, detail=str(exc))


async def _resolve_namespace(
    *, scope: Literal["account", "project", "workspace"], workspace_id: int | None, token_info: dict
) -> tuple[str | None, int | None]:
    if scope == "account":
        if token_info["auth_type"] == "api_key":
            raise HTTPException(status_code=403, detail="API 키는 계정 메모리에 접근할 수 없습니다")
        return None, workspace_id
    project_id = token_info["project_id"]
    if scope == "workspace":
        if workspace_id is None:
            raise ms.MemoryValidationError("workspace scope 에 workspace_id 가 필요합니다")
        try:
            await ws.get_workspace(workspace_id, user_id=token_info["user_id"])
        except ws.WorkspaceNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ws.WorkspaceForbidden as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    return project_id, workspace_id


@router.post("/memories", status_code=201)
async def create_memory(payload: MemoryCreate, token_info: dict = Depends(require_scopes("native:memory:write"))):
    try:
        project_id, workspace_id = await _resolve_namespace(
            scope=payload.scope, workspace_id=payload.workspace_id, token_info=token_info
        )
        return await ms.create_memory(
            user_id=token_info["user_id"],
            content=payload.content,
            scope=payload.scope,
            project_id=project_id,
            workspace_id=workspace_id,
            category=payload.category,
        )
    except (ms.MemoryValidationError, ms.ChatStorageUnavailable) as exc:
        raise _map_error(exc) from exc


@router.get("/memories")
async def list_memories(token_info: dict = Depends(require_scopes("native:memory:read"))):
    # 선택적 기능 목록: 저장소 미가용/데이터 없음은 빈 목록으로 graceful 처리(503 아님).
    try:
        return await ms.list_memories(
            user_id=token_info["user_id"],
            project_id=token_info["project_id"],
            **({"include_account": False} if token_info["auth_type"] == "api_key" else {}),
        )
    except ms.ChatStorageUnavailable:
        return []


@router.post("/memories/search")
async def search_memories(payload: MemorySearch, token_info: dict = Depends(require_scopes("native:memory:read"))):
    """User-initiated semantic search; vector IDs are rechecked in MySQL before disclosure."""
    try:
        if not semantic_memory_available():
            raise SemanticMemoryUnavailable("semantic memory index is unavailable")
        project_id, workspace_id = await _resolve_namespace(
            scope=payload.scope, workspace_id=payload.workspace_id, token_info=token_info
        )
        settings = get_settings()
        route = await ps.resolve_model(settings.chat_memory_embedding_model)
        if route is None:
            raise SemanticMemoryUnavailable("semantic memory embedding route is unavailable")
        ids = await mr.candidate_ids(
            query=payload.query,
            embedding_route=route,
            dimensions=settings.chat_memory_embedding_dimensions,
            user_id=token_info["user_id"],
            project_id=project_id,
            workspace_id=workspace_id,
            limit=settings.chat_memory_candidate_limit,
        )
        return await ms.hydrate_candidate_ids(
            ids=ids,
            user_id=token_info["user_id"],
            scope=payload.scope,
            project_id=project_id,
            workspace_id=workspace_id,
        )
    except (ms.MemoryValidationError, ms.ChatStorageUnavailable) as exc:
        raise _map_error(exc) from exc
    except SemanticMemoryUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.patch("/memories/{memory_id}")
async def update_memory(
    memory_id: int, payload: MemoryUpdate, token_info: dict = Depends(require_scopes("native:memory:write"))
):
    try:
        return await ms.update_memory(
            memory_id,
            user_id=token_info["user_id"],
            project_id=token_info["project_id"],
            patch=payload.model_dump(exclude_unset=True),
            **({"include_account": False} if token_info["auth_type"] == "api_key" else {}),
        )
    except (ms.MemoryNotFound, ms.MemoryForbidden, ms.MemoryValidationError, ms.ChatStorageUnavailable) as exc:
        raise _map_error(exc) from exc


@router.delete("/memories/{memory_id}", status_code=204)
async def delete_memory(memory_id: int, token_info: dict = Depends(require_scopes("native:memory:write"))):
    try:
        await ms.delete_memory(
            memory_id,
            user_id=token_info["user_id"],
            project_id=token_info["project_id"],
            **({"include_account": False} if token_info["auth_type"] == "api_key" else {}),
        )
    except (ms.MemoryNotFound, ms.MemoryForbidden, ms.ChatStorageUnavailable) as exc:
        raise _map_error(exc) from exc
