"""빌트인 AI 채팅 대화/메시지 API (사용자 소유 리소스).

전 엔드포인트 get_token_info 인증 + user_id 소유권 검증(IDOR 방어, 프로젝트 무관).
서비스(conversation_store)가 소유권을 강제하고, 예외를 HTTP 상태로 매핑한다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from lumen.auth import require_scopes
from lumen.config import get_settings
from lumen.services import capabilities
from lumen.services import conversation_store as cs
from lumen.services.providers import audio_transport, errors, image_transport, realtime_transport, repository, routing

router = APIRouter()


class AvailableModel(BaseModel):
    id: int
    model_name: str
    api_model_name: str
    api_provider: str
    provider_id: int
    provider_type: str
    provider_sort_order: int = 0
    sort_order: int = 0
    model_kind: Literal["text", "image", "tts", "stt", "realtime"] = "text"
    display_name: str
    provider: str | None = None
    provider_api_key_configured: bool
    # 유효 능력(vision/reasoning/tool_call/attachment/modalities/reasoning_options/context_limit).
    # 키·가격은 미포함이지만 능력은 배지·게이팅용으로 노출.
    capabilities: dict | None = None
    context_limit: int | None = None
    # Whether reasoning_effort="none" passes chat admission for this model (same rule).
    reasoning_none_supported: bool = False

    model_config = {"protected_namespaces": ()}


@router.get("/chat/models", response_model=list[AvailableModel])
async def list_available_models(
    model_kind: Literal["text", "image", "tts", "stt", "realtime"] = "text",
    token_info: dict = Depends(require_scopes("models:read")),
):
    """사용자용 활성 모델 카탈로그(키·가격 미포함, provider명·능력 포함). 저장소 장애 시 빈 목록(graceful)."""
    try:
        models = await repository.list_models(active_only=True)
        providers = {p["id"]: p for p in await repository.list_providers()}
    except errors.ChatStorageUnavailable:
        return []
    result = []
    for m in models:
        if m.get("model_kind", "text") != model_kind:
            continue
        caps = m.get("effective_capabilities") or {}
        provider = providers.get(m["provider_id"])
        provider_type = m.get("provider_type") or (provider.get("provider_type", "") if provider else "")
        result.append(
            {
                "id": m["id"],
                "model_name": m["model_name"],
                "api_model_name": m["api_model_name"],
                "model_kind": m.get("model_kind", "text"),
                "api_provider": m["api_provider"],
                "provider_id": m["provider_id"],
                "provider_type": provider_type,
                "provider_sort_order": m.get("provider_sort_order", 0),
                "sort_order": m.get("sort_order", 0),
                "display_name": m["display_name"],
                "provider": provider.get("name") if provider else None,
                "provider_api_key_configured": bool(provider and provider.get("has_api_key")),
                "capabilities": caps or None,
                "context_limit": caps.get("context_limit") if isinstance(caps, dict) else None,
                "reasoning_none_supported": capabilities.reasoning_can_be_disabled(
                    caps if isinstance(caps, dict) else None,
                    provider_type,
                ),
            }
        )
    return result


@router.get("/capabilities")
async def get_chat_capabilities(
    model_id: int = Query(..., ge=1),
    model_kind: Literal["text", "image", "tts", "stt", "realtime"] = "text",
    token_info: dict = Depends(require_scopes("models:read")),
):
    """Expose selected-model and deployment runtime gates without secrets."""
    del token_info
    try:
        resolved = (
            await routing.resolve_model_by_id(model_id)
            if model_kind == "text" else await routing.resolve_model_by_id(model_id, model_kind=model_kind)
        )
    except errors.ChatStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail="chat model configuration is unavailable") from exc
    if resolved is None:
        raise HTTPException(status_code=404, detail="chat model not found")
    runtime = capabilities.runtime_capabilities()
    effective = capabilities.effective_runtime_capabilities(resolved.get("capabilities"), runtime)
    result = {
        "model_id": model_id,
        "model_name": resolved["model_name"],
        "runtime": effective,
    }
    if model_kind != "text":
        result["model_kind"] = model_kind
        result["model_capabilities"] = resolved.get("capabilities")
    if model_kind == "image":
        image_gate = ((resolved.get("capabilities") or {}).get("feature_gates") or {}).get("image_output") or {}
        routable = image_gate.get("available") is True
        result["available_image_variants"] = image_transport.available_image_variants(resolved) if routable else []
        result["max_image_count"] = image_transport.max_image_count(resolved) if routable else 0
    if model_kind == "tts":
        audio_gate = ((resolved.get("capabilities") or {}).get("feature_gates") or {}).get("audio_output") or {}
        if audio_gate.get("available") is True:
            voices, formats = audio_transport.available_speech_options(resolved)
        else:
            voices, formats = [], []
        result["available_voices"] = voices
        result["available_formats"] = formats
    if model_kind == "stt":
        audio_gate = ((resolved.get("capabilities") or {}).get("feature_gates") or {}).get("audio_input") or {}
        result["available_timestamp_granularities"] = (
            audio_transport.available_timestamp_granularities(resolved) if audio_gate.get("available") is True else []
        )
    if model_kind == "realtime":
        gates = (resolved.get("capabilities") or {}).get("feature_gates") or {}
        if all((gates.get(name) or {}).get("available") is True for name in ("audio_input", "audio_output")):
            result.update(realtime_transport.available_realtime_options(resolved))
        else:
            result.update(available_voices=[], default_voice=None, input_sample_rate_hz=0,
                          output_sample_rate_hz=0, max_duration_seconds=0, default_duration_seconds=0)
    return result


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
    title_source: Literal["legacy", "auto", "explicit"]
    title_status: Literal["idle", "pending", "ready", "failed", "unavailable"]
    title_revision: int
    model_name: str | None
    workspace_id: int | None = None
    active_leaf_id: int | None = None
    history_revision: int = 0
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
    position: int | None = None
    branch: dict[str, int | None] | None = None

    model_config = {"protected_namespaces": ()}


class MessagePageResponse(BaseModel):
    """Bounded bidirectional page from the selected conversation path."""

    messages: list[MessageResponse]
    active_leaf_id: int | None = None
    history_revision: int
    has_before: bool = False
    has_after: bool = False
    before_cursor: str | None = None
    after_cursor: str | None = None


class ActiveLeafRequest(BaseModel):
    message_id: int
    descend: bool = False


class ForkRequest(BaseModel):
    message_id: int


def _encode_history_cursor(
    *, conversation_id: str, revision: int, direction: Literal["before", "after"], position: int
) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "conversation_id": conversation_id,
            "revision": revision,
            "direction": direction,
            "position": position,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    secret = bytes.fromhex(get_settings().get_lumen_encryption_key)
    signature = hmac.new(secret, payload, hashlib.sha256).digest()
    encoded_payload = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    return f"{encoded_payload}.{encoded_signature}"


def _decode_history_cursor(cursor: str, *, conversation_id: str) -> dict:
    if not cursor or len(cursor) > 512:
        raise ValueError("invalid cursor")
    try:
        encoded_payload, encoded_signature = cursor.split(".", 1)
        payload = base64.urlsafe_b64decode(encoded_payload + "=" * (-len(encoded_payload) % 4))
        signature = base64.urlsafe_b64decode(encoded_signature + "=" * (-len(encoded_signature) % 4))
        expected = hmac.new(bytes.fromhex(get_settings().get_lumen_encryption_key), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid signature")
        decoded = json.loads(payload)
    except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid cursor") from exc
    if (
        not isinstance(decoded, dict)
        or set(decoded) != {"v", "conversation_id", "revision", "direction", "position"}
        or decoded.get("v") != 1
        or decoded.get("conversation_id") != conversation_id
        or decoded.get("direction") not in {"before", "after"}
        or not isinstance(decoded.get("revision"), int)
        or not isinstance(decoded.get("position"), int)
        or decoded["revision"] < 0
        or decoded["position"] < 0
    ):
        raise ValueError("invalid cursor")
    return decoded


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (cs.ConversationNotFound, cs.WorkspaceNotFound)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (cs.ConversationForbidden, cs.WorkspaceForbidden)):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (cs.ConversationRunActive, cs.HistoryRevisionChanged)):
        return HTTPException(status_code=409, detail=exc.code)
    if isinstance(exc, cs.HistoryIndexUnavailable):
        return HTTPException(status_code=503, detail=exc.code)
    return HTTPException(status_code=503, detail=str(exc))


@router.post("/conversations", response_model=ConversationResponse, status_code=201)
async def create_conversation(
    payload: ConversationCreateRequest, token_info: dict = Depends(require_scopes("native:conversations:write"))
):
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
    conversation_id: str,
    payload: WorkspaceAssignRequest,
    token_info: dict = Depends(require_scopes("native:conversations:write")),
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
async def list_conversations(
    limit: int = 50, offset: int = 0, token_info: dict = Depends(require_scopes("native:conversations:read"))
):
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
    token_info: dict = Depends(require_scopes("native:conversations:read")),
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
async def get_conversation(
    conversation_id: str, token_info: dict = Depends(require_scopes("native:conversations:read"))
):
    try:
        return await cs.get_conversation(
            conversation_id, user_id=token_info["user_id"], project_id=token_info["project_id"]
        )
    except (cs.ConversationNotFound, cs.ConversationForbidden, cs.ChatStorageUnavailable) as exc:
        raise _map_error(exc) from exc


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: str, token_info: dict = Depends(require_scopes("native:conversations:write"))
):
    try:
        await cs.delete_conversation(
            conversation_id, user_id=token_info["user_id"], project_id=token_info["project_id"]
        )
    except (
        cs.ConversationNotFound,
        cs.ConversationForbidden,
        cs.ConversationRunActive,
        cs.ChatStorageUnavailable,
    ) as exc:
        raise _map_error(exc) from exc


@router.get("/conversations/{conversation_id}/messages", response_model=MessagePageResponse)
async def list_messages(
    conversation_id: str,
    anchor: Literal["latest", "first"] | None = Query(default=None),
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=40, ge=1, le=100),
    token_info: dict = Depends(require_scopes("native:conversations:read")),
):
    """Return an indexed page; cursor boundaries are exclusive and revision-fenced."""
    try:
        await cs.get_conversation(
            conversation_id,
            user_id=token_info["user_id"],
            project_id=token_info["project_id"],
        )
        if cursor is not None and anchor is not None:
            raise HTTPException(status_code=422, detail="anchor_and_cursor_are_mutually_exclusive")
        decoded = None
        if cursor is not None:
            try:
                decoded = _decode_history_cursor(cursor, conversation_id=conversation_id)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="invalid_history_cursor") from exc
        page = await cs.list_message_page(
            conversation_id,
            user_id=token_info["user_id"],
            project_id=token_info["project_id"],
            anchor=anchor or "latest",
            cursor_direction=decoded["direction"] if decoded else None,
            cursor_position=decoded["position"] if decoded else None,
            expected_revision=decoded["revision"] if decoded else None,
            limit=limit,
        )
        revision = page["history_revision"]
        before_position = page.pop("before_position")
        after_position = page.pop("after_position")
        page["before_cursor"] = (
            _encode_history_cursor(
                conversation_id=conversation_id,
                revision=revision,
                direction="before",
                position=before_position,
            )
            if before_position is not None
            else None
        )
        page["after_cursor"] = (
            _encode_history_cursor(
                conversation_id=conversation_id,
                revision=revision,
                direction="after",
                position=after_position,
            )
            if after_position is not None
            else None
        )
        return page
    except HTTPException:
        raise
    except (
        cs.ConversationNotFound,
        cs.ConversationForbidden,
        cs.HistoryIndexUnavailable,
        cs.HistoryRevisionChanged,
        cs.ChatStorageUnavailable,
    ) as exc:
        raise _map_error(exc) from exc


@router.patch("/conversations/{conversation_id}/active-leaf", response_model=ConversationResponse)
async def set_active_leaf(
    conversation_id: str,
    payload: ActiveLeafRequest,
    token_info: dict = Depends(require_scopes("native:conversations:write")),
):
    """형제 버전 전환 — 활성 리프를 지정 메시지로 이동."""
    try:
        return await cs.set_active_leaf(
            conversation_id,
            user_id=token_info["user_id"],
            project_id=token_info["project_id"],
            message_id=payload.message_id,
            descend=payload.descend,
        )
    except (
        cs.ConversationNotFound,
        cs.ConversationForbidden,
        cs.ConversationRunActive,
        cs.ChatStorageUnavailable,
    ) as exc:
        raise _map_error(exc) from exc


@router.post("/conversations/{conversation_id}/fork", response_model=ConversationResponse, status_code=201)
async def fork_conversation(
    conversation_id: str, payload: ForkRequest, token_info: dict = Depends(require_scopes("native:conversations:write"))
):
    """Create an independent conversation view sharing the selected immutable ancestry."""
    try:
        return await cs.fork_conversation(
            conversation_id,
            user_id=token_info["user_id"],
            project_id=token_info["project_id"],
            message_id=payload.message_id,
        )
    except (
        cs.ConversationNotFound,
        cs.ConversationForbidden,
        cs.ConversationRunActive,
        cs.HistoryIndexUnavailable,
        cs.ChatStorageUnavailable,
    ) as exc:
        raise _map_error(exc) from exc
