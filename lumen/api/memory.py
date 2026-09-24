"""Scoped user memory API.

The request project determines all project/workspace namespaces; clients never
submit an OpenStack project identifier. Every mutation/list/search route goes
through the selected ``lumen.memory`` plugin (``MemoryProvider``) with a
core-owned ``MemoryAccess``; authorization, encryption and the atomic
mutation+outbox transaction stay entirely inside ``memory_store``/
``MemoryHost`` regardless of which provider is selected.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from lumen_plugin_api.contracts import Namespace, PluginError
from lumen_plugin_api.memory import MemoryMutation
from pydantic import BaseModel, Field

from lumen.auth import require_scopes
from lumen.config import get_settings
from lumen.plugins import memory_host
from lumen.plugins.registry import get_plugin
from lumen.services import memory_retrieval as mr
from lumen.services import memory_store as ms
from lumen.services import workspace_store as ws
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


class MemoryDocument(BaseModel):
    filename: Literal["memory.md"] = "memory.md"
    content_type: Literal["text/markdown"] = "text/markdown"
    content: str


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ms.MemoryNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ms.MemoryForbidden):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ms.MemoryValidationError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=503, detail=str(exc))


def _plugin_http(exc: PluginError) -> HTTPException:
    if exc.code in {"plugin_unavailable", "plugin_incompatible"}:
        return HTTPException(status_code=503, detail=exc.code)
    if exc.code == "plugin_authority_revoked":
        return HTTPException(status_code=403, detail=exc.code)
    return HTTPException(status_code=422, detail=exc.code)


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


def _namespace(*, project_id: str | None, workspace_id: int | None, token_info: dict) -> Namespace:
    return Namespace(
        user_id=token_info["user_id"],
        project_id=project_id,
        workspace_id=workspace_id,
        include_account=token_info["auth_type"] != "api_key",
    )


@router.post("/memories", status_code=201)
async def create_memory(payload: MemoryCreate, token_info: dict = Depends(require_scopes("native:memory:write"))):
    try:
        project_id, workspace_id = await _resolve_namespace(
            scope=payload.scope, workspace_id=payload.workspace_id, token_info=token_info
        )
        namespace = _namespace(project_id=project_id, workspace_id=workspace_id, token_info=token_info)
        mutation = MemoryMutation(content=payload.content, category=payload.category, scope=payload.scope)
        provider = get_plugin("memory")
        return await provider.create(mutation, memory_host.access_for(namespace))
    except (ms.MemoryValidationError, ms.ChatStorageUnavailable) as exc:
        raise _map_error(exc) from exc
    except PluginError as exc:
        raise _plugin_http(exc) from exc


@router.get("/memories")
async def list_memories(token_info: dict = Depends(require_scopes("native:memory:read"))):
    # 선택적 기능 목록: 저장소 미가용/데이터 없음은 빈 목록으로 graceful 처리(503 아님).
    try:
        namespace = _namespace(project_id=token_info["project_id"], workspace_id=None, token_info=token_info)
        provider = get_plugin("memory")
        return await provider.list(memory_host.access_for(namespace))
    except ms.ChatStorageUnavailable:
        return []


@router.get("/memories/document", response_model=MemoryDocument)
async def get_memory_document(token_info: dict = Depends(require_scopes("native:memory:read"))):
    try:
        namespace = _namespace(project_id=token_info["project_id"], workspace_id=None, token_info=token_info)
        provider = get_plugin("memory")
        memories = await provider.list(memory_host.access_for(namespace))
    except ms.ChatStorageUnavailable:
        memories = []
    return MemoryDocument(content=ms.render_memory_markdown(memories))


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
        ids = await mr.candidate_ids(
            query=payload.query,
            user_id=token_info["user_id"],
            project_id=project_id,
            workspace_id=workspace_id,
            limit=min(30, max(1, settings.chat_memory_candidate_limit)),
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
    except PluginError as exc:
        raise _plugin_http(exc) from exc


@router.patch("/memories/{memory_id}")
async def update_memory(
    memory_id: int, payload: MemoryUpdate, token_info: dict = Depends(require_scopes("native:memory:write"))
):
    try:
        namespace = _namespace(project_id=token_info["project_id"], workspace_id=None, token_info=token_info)
        mutation = MemoryMutation(content=payload.content, category=payload.category, is_active=payload.is_active)
        provider = get_plugin("memory")
        return await provider.update(memory_id, mutation, memory_host.access_for(namespace))
    except (ms.MemoryNotFound, ms.MemoryForbidden, ms.MemoryValidationError, ms.ChatStorageUnavailable) as exc:
        raise _map_error(exc) from exc
    except PluginError as exc:
        raise _plugin_http(exc) from exc


@router.delete("/memories/{memory_id}", status_code=204)
async def delete_memory(memory_id: int, token_info: dict = Depends(require_scopes("native:memory:write"))):
    try:
        namespace = _namespace(project_id=token_info["project_id"], workspace_id=None, token_info=token_info)
        provider = get_plugin("memory")
        await provider.delete(memory_id, memory_host.access_for(namespace))
    except (ms.MemoryNotFound, ms.MemoryForbidden, ms.ChatStorageUnavailable) as exc:
        raise _map_error(exc) from exc
    except PluginError as exc:
        raise _plugin_http(exc) from exc
