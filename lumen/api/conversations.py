"""빌트인 AI 채팅 대화/메시지 API (사용자 소유 리소스).

전 엔드포인트 get_token_info 인증 + user_id 소유권 검증(IDOR 방어, 프로젝트 무관).
서비스(conversation_store)가 소유권을 강제하고, 예외를 HTTP 상태로 매핑한다.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from lumen.auth import get_token_info
from lumen.services import capabilities, provider_store
from lumen.services import conversation_store as cs

router = APIRouter()


class AvailableModel(BaseModel):
    id: int
    model_name: str
    display_name: str
    provider: str | None = None
    # 유효 능력(vision/reasoning/tool_call/attachment/modalities/reasoning_options/context_limit).
    # 키·가격은 미포함이지만 능력은 배지·게이팅용으로 노출.
    capabilities: dict | None = None
    context_limit: int | None = None

    model_config = {"protected_namespaces": ()}


@router.get("/chat/models", response_model=list[AvailableModel])
async def list_available_models(token_info: dict = Depends(get_token_info)):
    """사용자용 활성 모델 카탈로그(키·가격 미포함, provider명·능력 포함). 저장소 장애 시 빈 목록(graceful)."""
    try:
        models = await provider_store.list_models(active_only=True)
        providers = {p["id"]: p["name"] for p in await provider_store.list_providers()}
    except provider_store.ChatStorageUnavailable:
        return []
    result = []
    for m in models:
        caps = m.get("effective_capabilities") or {}
        result.append(
            {
                "id": m["id"],
                "model_name": m["model_name"],
                "display_name": m.get("display_name") or m["model_name"],
                "provider": providers.get(m["provider_id"]),
                "capabilities": caps or None,
                "context_limit": caps.get("context_limit") if isinstance(caps, dict) else None,
            }
        )
    return result


@router.get("/capabilities")
async def get_chat_capabilities(model_id: int = Query(..., ge=1), token_info: dict = Depends(get_token_info)):
    """Expose selected-model and deployment runtime gates without secrets."""
    del token_info
    try:
        resolved = await provider_store.resolve_model_by_id(model_id)
    except provider_store.ChatStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail="chat model configuration is unavailable") from exc
    if resolved is None:
        raise HTTPException(status_code=404, detail="chat model not found")
    runtime = capabilities.runtime_capabilities()
    effective = capabilities.effective_runtime_capabilities(resolved.get("capabilities"), runtime)
    return {
        "model_id": model_id,
        "model_name": resolved["model_name"],
        "runtime": effective,
    }


class ConversationCreateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    model_name: str | None = Field(default=None, max_length=190)
    workspace_id: int | None = Field(default=None)  # 소속 프로젝트(workspace)

    model_config = {"protected_namespaces": ()}


class WorkspaceAssignRequest(BaseModel):
    workspace_id: int | None = None  # None 이면 프로젝트에서 제외


class ConversationResponse(BaseModel):
    id: str
    project_id: str
    user_id: str
    title: str | None
    model_name: str | None
    workspace_id: int | None = None
    active_leaf_id: int | None = None
    parent_conversation_id: str | None = None
    forked_from_message_id: int | None = None
    created_at: str | None
    updated_at: str | None

    model_config = {"protected_namespaces": ()}


class MessageResponse(BaseModel):
    id: int
    conversation_id: str
    role: str
    parent_id: int | None = None
    content: str | None
    tool_calls: list | None
    citations: list | None = None
    reasoning: str | None = None
    parts: list | None = None
    status: str | None = None
    execution: dict | None = None
    token_prompt: int
    token_completion: int
    model_name: str | None = None
    created_at: str | None
    created_at_local: str | None = None
    created_timezone: str | None = None

    model_config = {"protected_namespaces": ()}


class MessageTreeNode(BaseModel):
    """Unencrypted branch metadata used for version navigation."""

    id: int
    parent_id: int | None
    role: str
    created_at: str | None


class MessageTreeResponse(BaseModel):
    """Backward page from a conversation message tree."""

    messages: list[MessageResponse]
    active_leaf_id: int | None = None
    has_more: bool = False
    tree_nodes: list[MessageTreeNode] = []
    next_before_id: int | None = None


class ActiveLeafRequest(BaseModel):
    message_id: int


class ForkRequest(BaseModel):
    message_id: int


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (cs.ConversationNotFound, cs.WorkspaceNotFound)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (cs.ConversationForbidden, cs.WorkspaceForbidden)):
        return HTTPException(status_code=403, detail=str(exc))
    return HTTPException(status_code=503, detail=str(exc))


@router.post("/conversations", response_model=ConversationResponse, status_code=201)
async def create_conversation(payload: ConversationCreateRequest, token_info: dict = Depends(get_token_info)):
    try:
        return await cs.create_conversation(
            project_id=token_info["project_id"],
            user_id=token_info["user_id"],
            title=payload.title,
            model_name=payload.model_name,
            workspace_id=payload.workspace_id,
        )
    except (cs.WorkspaceNotFound, cs.WorkspaceForbidden, cs.ChatStorageUnavailable) as exc:
        raise _map_error(exc) from exc


@router.patch("/conversations/{conversation_id}/workspace", response_model=ConversationResponse)
async def set_conversation_workspace(
    conversation_id: str, payload: WorkspaceAssignRequest, token_info: dict = Depends(get_token_info)
):
    """대화를 프로젝트(workspace)에 배정하거나 해제(None)."""
    try:
        return await cs.set_workspace(
            conversation_id,
            user_id=token_info["user_id"],
            project_id=token_info["project_id"],
            workspace_id=payload.workspace_id,
        )
    except (
        cs.ConversationNotFound,
        cs.ConversationForbidden,
        cs.WorkspaceNotFound,
        cs.WorkspaceForbidden,
        cs.ChatStorageUnavailable,
    ) as exc:
        raise _map_error(exc) from exc


@router.get("/conversations", response_model=list[ConversationResponse])
async def list_conversations(limit: int = 50, offset: int = 0, token_info: dict = Depends(get_token_info)):
    try:
        return await cs.list_conversations(
            user_id=token_info["user_id"], project_id=token_info["project_id"], limit=limit, offset=offset
        )
    except cs.ChatStorageUnavailable as exc:
        raise _map_error(exc) from exc


@router.get("/conversations/search", response_model=list[ConversationResponse])
async def search_conversations(
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=20, ge=1, le=50),
    token_info: dict = Depends(get_token_info),
):
    try:
        return await cs.search_conversations(
            user_id=token_info["user_id"],
            project_id=token_info["project_id"],
            query=q,
            limit=limit,
        )
    except cs.ChatStorageUnavailable as exc:
        raise _map_error(exc) from exc


@router.get("/conversations/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(conversation_id: str, token_info: dict = Depends(get_token_info)):
    try:
        return await cs.get_conversation(
            conversation_id, user_id=token_info["user_id"], project_id=token_info["project_id"]
        )
    except (cs.ConversationNotFound, cs.ConversationForbidden, cs.ChatStorageUnavailable) as exc:
        raise _map_error(exc) from exc


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: str, token_info: dict = Depends(get_token_info)):
    try:
        await cs.delete_conversation(
            conversation_id, user_id=token_info["user_id"], project_id=token_info["project_id"]
        )
    except (cs.ConversationNotFound, cs.ConversationForbidden, cs.ChatStorageUnavailable) as exc:
        raise _map_error(exc) from exc


@router.get("/conversations/{conversation_id}/messages", response_model=MessageTreeResponse)
async def list_messages(
    conversation_id: str,
    before_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=40, ge=1, le=100),
    token_info: dict = Depends(get_token_info),
):
    """Return a bounded active-branch page; before_id anchors the next ancestor page."""
    try:
        return await cs.list_message_tree(
            conversation_id,
            user_id=token_info["user_id"],
            project_id=token_info["project_id"],
            before_id=before_id,
            limit=limit,
        )
    except (cs.ConversationNotFound, cs.ConversationForbidden, cs.ChatStorageUnavailable) as exc:
        raise _map_error(exc) from exc


@router.patch("/conversations/{conversation_id}/active-leaf", response_model=ConversationResponse)
async def set_active_leaf(conversation_id: str, payload: ActiveLeafRequest, token_info: dict = Depends(get_token_info)):
    """형제 버전 전환 — 활성 리프를 지정 메시지로 이동."""
    try:
        return await cs.set_active_leaf(
            conversation_id,
            user_id=token_info["user_id"],
            project_id=token_info["project_id"],
            message_id=payload.message_id,
        )
    except (cs.ConversationNotFound, cs.ConversationForbidden, cs.ChatStorageUnavailable) as exc:
        raise _map_error(exc) from exc


@router.post("/conversations/{conversation_id}/fork", response_model=ConversationResponse, status_code=201)
async def fork_conversation(conversation_id: str, payload: ForkRequest, token_info: dict = Depends(get_token_info)):
    """지정 메시지까지의 경로를 새 대화로 복사(분기). 소유자 동일, 원본 독립."""
    try:
        return await cs.fork_conversation(
            conversation_id,
            user_id=token_info["user_id"],
            project_id=token_info["project_id"],
            message_id=payload.message_id,
        )
    except (cs.ConversationNotFound, cs.ConversationForbidden, cs.ChatStorageUnavailable) as exc:
        raise _map_error(exc) from exc
