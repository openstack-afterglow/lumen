"""빌트인 AI 채팅 스트리밍 completions (SSE) — 메시지 버전 트리.

흐름: precheck(fail-closed) → 대화 소유권(user_id) → resolve_model 화이트리스트 → 활성 경로 로드 →
user 메시지 저장(parent=active_leaf) → engine.stream → parent 체인으로 메시지 저장 + active_leaf 갱신 + 과금.
재생성은 대상 답변의 턴-시작 user 아래에 새 assistant 형제를 만든다(다른 모델 가능, active_leaf 이동).

⚠️ 과금은 정상 완료·중단 무관하게 정확히 1회. 모델 replay 에서 role=tool 은 제외(orphaned tool 400 방지).
"""

from __future__ import annotations

import asyncio
import json
import logging
from time import monotonic
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from lumen.auth import Principal, require_scopes
from lumen.config import get_settings
from lumen.models.chat_contracts import (
    ChatFeatureOptions,
    ChatRunDescriptor,
    ChatRunResponse,
    CompactionRequest,
    CompletionRequest,
    ContextPreviewRequest,
    ContextState,
    RegenerateRequest,
    RunInteractionResponseRequest,
    TempCompletionRequest,
    ToolApprovalDecisionRequest,
    text_projection_from_user_input_parts,
    validate_user_input_parts,
)
from lumen.models.chat_runs import ChatRun
from lumen.services import agent_policy, credit
from lumen.services import conversation_store as cs
from lumen.services.chat_admission import (
    _load_owned_conv,
    prepare_context_input,
)
from lumen.services.durable_runs import admission, common, interactions, lifecycle, queries
from lumen.services.durable_runs import errors as durable_errors
from lumen.services.run_store import NONTERMINAL

logger = logging.getLogger(__name__)
router = APIRouter()

_MAX_TOKENS_CAP = 4096
_MAX_MESSAGE_CHARS = 32000


def _active_leaf_fence(value: object) -> int | None:
    """Normalize context-store's JSON string IDs to the DB integer key type."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail="context_revision_changed") from exc


def _user_input_parts_from_message(message: dict) -> list:
    """Restore API input refs from a persisted canonical message projection."""
    raw_parts = message.get("parts")
    if isinstance(raw_parts, list) and raw_parts:
        converted: list[dict] = []
        for part in raw_parts:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type == "text" and isinstance(part.get("text"), str):
                converted.append({"type": "text", "text": part["text"]})
            elif part_type in {"image", "audio", "video", "document"} and isinstance(part.get("asset_id"), str):
                converted.append({"type": part_type, "asset_id": part["asset_id"]})
            elif part_type == "file" and isinstance(part.get("asset_id"), str):
                mime = str(part.get("mime_type") or "")
                inferred = "document" if mime == "application/pdf" else mime.partition("/")[0]
                if inferred in {"image", "audio", "video", "document"}:
                    converted.append({"type": inferred, "asset_id": part["asset_id"]})
        if converted:
            return validate_user_input_parts(converted)
    content = message.get("content")
    if isinstance(content, str) and content:
        return validate_user_input_parts([{"type": "text", "text": content}])
    raise HTTPException(status_code=422, detail="재시도할 사용자 입력을 찾을 수 없습니다")


def _features_payload(features: ChatFeatureOptions) -> dict:
    return features.model_dump(mode="json", by_alias=True)


def _admission_execution_protocol_version() -> int:
    """Admit only workers that this API deployment can execute safely."""
    version = get_settings().chat_execution_protocol_version
    try:
        common._require_supported_execution_protocol_version(version)
    except durable_errors.DurableRunInputError as exc:
        raise HTTPException(status_code=503, detail="configured chat execution protocol is not deployed") from exc
    return version


def _run_error(exc: durable_errors.DurableRunError) -> HTTPException:
    if isinstance(exc, durable_errors.DurableRunConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, durable_errors.DurableRunInputError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, durable_errors.DurableRunCursorExpired):
        return HTTPException(status_code=410, detail=str(exc))
    if isinstance(exc, durable_errors.DurableRunNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=503, detail=str(exc))


@router.post(
    "/conversations/{conversation_id}/completions",
    response_model=ChatRunDescriptor,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_completion(
    conversation_id: str,
    payload: CompletionRequest,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    token_info: Principal = Depends(require_scopes("native:conversations:write", "native:runs:write")),
):
    """Persist intent and return a descriptor; the worker owns all provider I/O."""
    user_id = token_info["user_id"]
    project_id = token_info["project_id"]
    try:
        message_text = text_projection_from_user_input_parts(payload.parts)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await _load_owned_conv(conversation_id, user_id, project_id)
    try:
        path = await cs.get_active_path(conversation_id, user_id=user_id, project_id=project_id)
        expected_active_leaf_id = _active_leaf_fence(path["active_leaf_id"])
        features = _features_payload(payload.features)
        intent = {
            "endpoint": "completion",
            "conversation_id": conversation_id,
            "parent_id": str(path["active_leaf_id"]) if path["active_leaf_id"] is not None else None,
            "parts": [part.model_dump(mode="json", by_alias=True) for part in payload.parts],
            "features": features,
            "agent_id": payload.agent_id,
            "execution_mode": payload.execution_mode,
            "code_workspace_id": payload.code_workspace_id,
            "reasoning_effort": payload.reasoning_effort,
            "skill_ids": payload.skill_ids,
            "client_timezone": payload.client_timezone,
        }
        intent.update(_api_key_id=token_info["api_key_id"], _source=token_info["source"])
        existing = await admission.existing_run_for_intent(
            project_id=project_id,
            user_id=user_id,
            client_request_id=str(idempotency_key),
            intent=intent,
            conversation_id=conversation_id,
        )
        if existing is not None:
            return existing
    except durable_errors.DurableRunError as exc:
        raise _run_error(exc) from exc
    except cs.ChatStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        await credit.precheck(user_id, project_id, api_key_id=token_info["api_key_id"])
    except credit.QuotaExceeded as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc
    except credit.ChatStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    execution_protocol_version = _admission_execution_protocol_version()

    planned = await prepare_context_input(
        user_id=user_id,
        project_id=project_id,
        conversation_id=conversation_id,
        temp_thread_id=None,
        model_id=payload.model_id,
        parts=payload.parts,
        features=payload.features,
        agent_id=int(payload.agent_id) if payload.agent_id else None,
        skill_ids=payload.skill_ids,
        execution_mode=payload.execution_mode,
        reasoning_effort=payload.reasoning_effort,
        code_workspace_id=payload.code_workspace_id,
        client_timezone=payload.client_timezone,
        token_info=token_info,
        is_preview=False,
    )

    agent = planned["agent"]
    try:
        execution_policy = (
            agent_policy.resolve_execution_policy(agent, execution_mode=payload.execution_mode)
            if execution_protocol_version == 2
            else None
        )
        direct_effects = (
            agent_policy.resolve_direct_effects(agent, execution_mode=payload.execution_mode)
            if execution_protocol_version == 2
            else None
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    capability_snapshot = dict(planned["capability_snapshot"])
    if execution_policy is not None and direct_effects is not None:
        capability_snapshot["execution_policy"] = execution_policy.model_dump(mode="json")
        capability_snapshot["direct_effects"] = sorted(direct_effects)
    capability_snapshot["execution_protocol_version"] = execution_protocol_version

    try:
        return await admission.create_persistent_run(
            project_id=project_id,
            user_id=user_id,
            client_request_id=str(idempotency_key),
            intent=intent,
            conversation_id=conversation_id,
            model_name=payload.model_id,
            agent_id=agent.get("id") if agent else None,
            user_content=message_text,
            user_parts=[part.model_dump(mode="json", by_alias=True) for part in payload.parts],
            client_timezone=payload.client_timezone,
            request_payload={
                "input_messages": planned["input_messages"],
                "input_parts": [part.model_dump(mode="json", by_alias=True) for part in payload.parts],
                "features": features,
                "skill_snapshot": planned["skill_snapshot"],
                "max_tokens": planned["max_tokens"],
                "temperature": planned["temperature"],
                "reasoning_effort": planned["reasoning_effort"],
                "client_timezone": payload.client_timezone,
                "skill_ids": payload.skill_ids,
                "extension_snapshot": planned["extension_selection"],
                "context_source": planned["source"],
                "tool_schemas": planned["tool_schemas"],
                **(
                    {
                        "execution_policy": execution_policy.model_dump(mode="json"),
                        "allowed_direct_effects": sorted(direct_effects),
                    }
                    if execution_policy is not None and direct_effects is not None
                    else {}
                ),
            },
            capability_snapshot=capability_snapshot,
            pricing_snapshot=planned["pricing_snapshot"],
            execution_protocol_version=execution_protocol_version,
            source=token_info["source"],
            api_key_id=token_info["api_key_id"],
            expected_parent_id=expected_active_leaf_id,
        )
    except durable_errors.DurableRunError as exc:
        raise _run_error(exc) from exc


@router.post(
    "/conversations/{conversation_id}/messages/{message_id}/regenerate",
    response_model=ChatRunDescriptor,
    status_code=status.HTTP_202_ACCEPTED,
)
async def regenerate_message(
    conversation_id: str,
    message_id: int,
    payload: RegenerateRequest,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    token_info: Principal = Depends(require_scopes("native:conversations:write", "native:runs:write")),
):
    project_id = token_info["project_id"]
    user_id = token_info["user_id"]
    await _load_owned_conv(conversation_id, user_id, project_id)
    try:
        turn_user = await cs.find_turn_start_user(
            conversation_id, user_id=user_id, project_id=project_id, message_id=message_id
        )
    except (cs.ConversationNotFound, cs.ConversationForbidden) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if turn_user is None:
        raise HTTPException(status_code=400, detail="재생성할 사용자 턴을 찾을 수 없습니다")
    try:
        source_parts = _user_input_parts_from_message(turn_user)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="저장된 사용자 입력이 유효하지 않습니다") from exc
    features = _features_payload(payload.features)
    intent = {
        "endpoint": "regenerate",
        "conversation_id": conversation_id,
        "parent_id": str(turn_user["id"]),
        "model_id": payload.model_id,
        "features": features,
        "client_timezone": payload.client_timezone,
        "reasoning_effort": payload.reasoning_effort,
    }
    intent.update(_api_key_id=token_info["api_key_id"], _source=token_info["source"])
    try:
        existing = await admission.existing_run_for_intent(
            project_id=project_id,
            user_id=user_id,
            client_request_id=str(idempotency_key),
            intent=intent,
            conversation_id=conversation_id,
        )
        if existing is not None:
            return existing
    except durable_errors.DurableRunError as exc:
        raise _run_error(exc) from exc
    try:
        # Regeneration reads the selected user ancestor, but the mutation must
        # fence the active branch that was visible when this request started.
        active_path = await cs.get_active_path(conversation_id, user_id=user_id, project_id=project_id)
        expected_active_leaf_id = _active_leaf_fence(active_path.get("active_leaf_id"))
    except cs.ChatStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail="chat storage is unavailable") from exc

    try:
        await credit.precheck(user_id, project_id, api_key_id=token_info["api_key_id"])
    except credit.QuotaExceeded as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc
    except credit.ChatStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    execution_protocol_version = _admission_execution_protocol_version()
    planned = await prepare_context_input(
        user_id=user_id,
        project_id=project_id,
        conversation_id=conversation_id,
        temp_thread_id=None,
        leaf_id=turn_user["id"],
        model_id=payload.model_id,
        parts=source_parts,
        features=payload.features,
        agent_id=None,
        skill_ids=[],
        execution_mode="chat",
        reasoning_effort=payload.reasoning_effort,
        code_workspace_id=None,
        client_timezone=payload.client_timezone,
        token_info=token_info,
        is_preview=False,
        append_draft=False,
    )

    capability_snapshot = dict(planned["capability_snapshot"])
    capability_snapshot["execution_protocol_version"] = execution_protocol_version

    try:
        return await admission.create_run(
            project_id=project_id,
            user_id=user_id,
            client_request_id=str(idempotency_key),
            intent=intent,
            conversation_id=conversation_id,
            temp_thread_id=None,
            model_name=payload.model_id,
            agent_id=None,
            user_message_id=turn_user["id"],
            request_payload={
                "input_messages": planned["input_messages"],
                "features": features,
                "max_tokens": planned["max_tokens"],
                "temperature": planned["temperature"],
                "reasoning_effort": planned["reasoning_effort"],
                "client_timezone": payload.client_timezone,
                "context_source": planned["source"],
                "tool_schemas": planned["tool_schemas"],
            },
            capability_snapshot=capability_snapshot,
            pricing_snapshot=planned["pricing_snapshot"],
            execution_protocol_version=execution_protocol_version,
            source=token_info["source"],
            api_key_id=token_info["api_key_id"],
            expected_parent_id=expected_active_leaf_id,
        )
    except durable_errors.DurableRunError as exc:
        raise _run_error(exc) from exc


@router.post(
    "/conversations/{conversation_id}/runs/{run_id}/retry",
    response_model=ChatRunDescriptor,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_failed_run(
    conversation_id: str,
    run_id: str,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    token_info: Principal = Depends(require_scopes("native:conversations:write", "native:runs:write")),
):
    project_id = token_info["project_id"]
    user_id = token_info["user_id"]
    await _load_owned_conv(conversation_id, user_id, project_id)

    intent = {
        "endpoint": "retry",
        "conversation_id": conversation_id,
        "source_run_id": run_id,
    }
    intent.update(_api_key_id=token_info["api_key_id"], _source=token_info["source"])
    try:
        existing = await admission.existing_run_for_intent(
            project_id=project_id,
            user_id=user_id,
            client_request_id=str(idempotency_key),
            intent=intent,
            conversation_id=conversation_id,
        )
        if existing is not None:
            return existing
    except durable_errors.DurableRunError as exc:
        raise _run_error(exc) from exc
    try:
        await credit.precheck(user_id, project_id, api_key_id=token_info["api_key_id"])
    except credit.QuotaExceeded as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc
    except credit.ChatStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    factory = common._factory()
    async with factory() as session:
        source_run = (
            (
                await session.execute(
                    select(ChatRun).where(
                        ChatRun.id == run_id,
                        ChatRun.conversation_id == conversation_id,
                        ChatRun.user_id == user_id,
                        ChatRun.project_id == project_id,
                    )
                )
            )
            .scalars()
            .first()
        )

    if source_run is None:
        raise HTTPException(status_code=404, detail="재시도할 실행 기록을 찾을 수 없습니다")

    if source_run.status not in {"failed", "canceled"}:
        raise HTTPException(status_code=400, detail="실패하거나 취소된 실행만 재시도할 수 있습니다")

    if source_run.user_message_id is None:
        raise HTTPException(status_code=400, detail="재시도할 사용자 메시지를 찾을 수 없습니다")

    message_id = source_run.user_message_id
    try:
        # Retry projects the failed run's saved user turn, but fences the
        # active leaf observed before planning so a branch switch cannot be
        # overwritten by a late retry.
        active_path = await cs.get_active_path(conversation_id, user_id=user_id, project_id=project_id)
        expected_active_leaf_id = _active_leaf_fence(active_path.get("active_leaf_id"))
    except cs.ChatStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail="chat storage is unavailable") from exc

    try:
        raw_payload = cs._dec(source_run.request_payload) if source_run.request_payload else "{}"
        payload_dict = json.loads(raw_payload or "{}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail="실행 정보를 복원할 수 없습니다") from exc

    model_name = source_run.model_name
    agent_id = source_run.agent_id
    raw_features = payload_dict.get("features")
    features_obj = (
        ChatFeatureOptions.model_validate(raw_features) if isinstance(raw_features, dict) else ChatFeatureOptions()
    )
    features_dict = _features_payload(features_obj)
    reasoning_effort = payload_dict.get("reasoning_effort", "auto")
    skill_ids = payload_dict.get("skill_ids", [])
    raw_parts = payload_dict.get("input_parts")
    path_messages = await cs.path_ending_at(
        conversation_id, user_id=user_id, project_id=project_id, message_id=message_id
    )
    if isinstance(raw_parts, list) and raw_parts:
        try:
            typed_parts = validate_user_input_parts(raw_parts)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="저장된 사용자 입력이 유효하지 않습니다") from exc
    else:
        target_msg = next((m for m in reversed(path_messages) if m["id"] == message_id), None)
        if target_msg is None:
            raise HTTPException(status_code=422, detail="재시도할 사용자 입력을 찾을 수 없습니다")
        try:
            typed_parts = _user_input_parts_from_message(target_msg)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="저장된 사용자 입력이 유효하지 않습니다") from exc
    user_parts = [part.model_dump(mode="json", by_alias=True) for part in typed_parts]
    execution_protocol_version = _admission_execution_protocol_version()
    planned = await prepare_context_input(
        user_id=user_id,
        project_id=project_id,
        conversation_id=conversation_id,
        temp_thread_id=None,
        leaf_id=message_id,
        model_id=model_name,
        parts=typed_parts,
        features=features_obj,
        agent_id=agent_id,
        skill_ids=skill_ids if isinstance(skill_ids, list) else [],
        execution_mode=source_run.execution_mode,
        reasoning_effort=reasoning_effort,
        code_workspace_id=None,
        client_timezone=None,
        token_info=token_info,
        is_preview=False,
        append_draft=False,
    )

    agent = planned["agent"]
    try:
        execution_policy = (
            agent_policy.resolve_execution_policy(agent, execution_mode=source_run.execution_mode)
            if execution_protocol_version == 2
            else None
        )
        direct_effects = (
            agent_policy.resolve_direct_effects(agent, execution_mode=source_run.execution_mode)
            if execution_protocol_version == 2
            else None
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        return await admission.create_run(
            project_id=project_id,
            user_id=user_id,
            client_request_id=str(idempotency_key),
            intent=intent,
            conversation_id=conversation_id,
            temp_thread_id=None,
            model_name=model_name,
            agent_id=agent.get("id") if agent else None,
            user_message_id=message_id,
            request_payload={
                "input_messages": planned["input_messages"],
                "input_parts": user_parts,
                "features": features_dict,
                "skill_snapshot": planned["skill_snapshot"],
                "max_tokens": planned["max_tokens"],
                "temperature": planned["temperature"],
                "reasoning_effort": planned["reasoning_effort"],
                "skill_ids": skill_ids,
                "extension_snapshot": planned["extension_selection"],
                "context_source": planned["source"],
                "tool_schemas": planned["tool_schemas"],
                **(
                    {
                        "execution_policy": execution_policy.model_dump(mode="json"),
                        "allowed_direct_effects": sorted(direct_effects),
                    }
                    if execution_policy is not None and direct_effects is not None
                    else {}
                ),
            },
            capability_snapshot=planned["capability_snapshot"],
            pricing_snapshot=planned["pricing_snapshot"],
            execution_protocol_version=execution_protocol_version,
            source=token_info["source"],
            api_key_id=token_info["api_key_id"],
            expected_parent_id=expected_active_leaf_id,
        )
    except durable_errors.DurableRunError as exc:
        raise _run_error(exc) from exc


@router.post("/temp-completions", response_model=ChatRunDescriptor, status_code=status.HTTP_202_ACCEPTED)
async def temp_completion(
    payload: TempCompletionRequest,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    token_info: Principal = Depends(require_scopes("native:runs:write")),
):
    user_id = token_info["user_id"]
    project_id = token_info["project_id"]
    try:
        text_projection_from_user_input_parts(payload.parts)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    features = _features_payload(payload.features)
    intent = {
        "endpoint": "temp_completion",
        "temp_thread_id": payload.temp_thread_id,
        "model_id": payload.model_id,
        "parts": [part.model_dump(mode="json", by_alias=True) for part in payload.parts],
        "features": features,
        "reasoning_effort": payload.reasoning_effort,
        "skill_ids": payload.skill_ids,
        "execution_mode": payload.execution_mode,
        "code_workspace_id": payload.code_workspace_id,
    }
    intent.update(_api_key_id=token_info["api_key_id"], _source=token_info["source"])
    try:
        existing = await admission.existing_run_for_intent(
            project_id=project_id,
            user_id=user_id,
            client_request_id=str(idempotency_key),
            intent=intent,
            conversation_id=None,
        )
        if existing is not None:
            return existing
        await credit.precheck(user_id, project_id, api_key_id=token_info["api_key_id"])
    except durable_errors.DurableRunError as exc:
        raise _run_error(exc) from exc
    except credit.QuotaExceeded as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc
    except credit.ChatStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    execution_protocol_version = _admission_execution_protocol_version()
    planned = await prepare_context_input(
        user_id=user_id,
        project_id=project_id,
        conversation_id=None,
        temp_thread_id=payload.temp_thread_id,
        model_id=payload.model_id,
        parts=payload.parts,
        features=payload.features,
        agent_id=None,
        skill_ids=payload.skill_ids,
        execution_mode=payload.execution_mode,
        reasoning_effort=payload.reasoning_effort,
        code_workspace_id=payload.code_workspace_id,
        client_timezone=None,
        token_info=token_info,
        is_preview=False,
        is_context_operation=False,
        allow_empty_source=payload.temp_thread_id is None,
    )

    capability_snapshot = dict(planned["capability_snapshot"])
    capability_snapshot["execution_protocol_version"] = execution_protocol_version

    try:
        return await admission.create_temp_run(
            project_id=project_id,
            user_id=user_id,
            client_request_id=str(idempotency_key),
            intent=intent,
            temp_thread_id=payload.temp_thread_id,
            model_name=payload.model_id,
            request_payload={
                "input_messages": planned["input_messages"],
                "input_parts": [part.model_dump(mode="json", by_alias=True) for part in payload.parts],
                "features": features,
                "max_tokens": planned["max_tokens"],
                "temperature": planned["temperature"],
                "reasoning_effort": planned["reasoning_effort"],
                "skill_snapshot": planned["skill_snapshot"],
                "skill_ids": payload.skill_ids,
                "extension_snapshot": planned["extension_selection"],
                "context_source": planned["source"],
                "tool_schemas": planned["tool_schemas"],
            },
            capability_snapshot=capability_snapshot,
            pricing_snapshot=planned["pricing_snapshot"],
            execution_protocol_version=execution_protocol_version,
            source=token_info["source"],
            api_key_id=token_info["api_key_id"],
        )
    except durable_errors.DurableRunError as exc:
        raise _run_error(exc) from exc


async def _active_context_compaction_run_id(
    *, conversation_id: str | None = None, temp_thread_id: str | None = None
) -> str | None:
    if (conversation_id is None) == (temp_thread_id is None):
        raise ValueError("exactly one context parent is required")
    try:
        factory = common._factory()
        async with factory() as session:
            query = select(ChatRun.id).where(
                ChatRun.status.in_(NONTERMINAL),
                ChatRun.run_kind == "compaction",
                ChatRun.conversation_id == conversation_id
                if conversation_id is not None
                else ChatRun.temp_thread_id == temp_thread_id,
            )
            run_id = (await session.execute(query)).scalar_one_or_none()
    except durable_errors.DurableRunError as exc:
        raise HTTPException(status_code=503, detail="chat storage is unavailable") from exc
    return str(run_id) if run_id is not None else None


@router.post(
    "/conversations/{conversation_id}/context-preview",
    response_model=ContextState,
    status_code=status.HTTP_200_OK,
)
async def preview_conversation_context(
    conversation_id: str,
    payload: ContextPreviewRequest,
    token_info: Principal = Depends(require_scopes("native:conversations:read", "models:read")),
):
    user_id = token_info["user_id"]
    project_id = token_info["project_id"]
    planned = await prepare_context_input(
        user_id=user_id,
        project_id=project_id,
        conversation_id=conversation_id,
        temp_thread_id=None,
        model_id=payload.model_id,
        parts=payload.parts,
        features=payload.features,
        agent_id=int(payload.agent_id) if payload.agent_id else None,
        skill_ids=payload.skill_ids,
        execution_mode=payload.execution_mode,
        reasoning_effort=payload.reasoning_effort,
        code_workspace_id=payload.code_workspace_id,
        client_timezone=payload.client_timezone,
        token_info=token_info,
        is_preview=True,
        is_context_operation=True,
    )
    from lumen.services import context_manager

    resolved = planned["resolved"]
    capabilities = resolved.get("capabilities") or {}
    context_limit = capabilities.get("context_limit")
    output_reserve = min(planned["max_tokens"], capabilities.get("max_output_tokens", 4096))
    source = planned["source"]
    active_compaction_run_id = await _active_context_compaction_run_id(conversation_id=conversation_id)

    return context_manager.context_state(
        planned["input_messages"],
        planned["tool_schemas"],
        model_name=resolved["model_name"],
        context_limit=context_limit,
        output_reserve=output_reserve,
        revision=source["revision"],
        checkpoint_id=source.get("checkpoint_id"),
        active_compaction_run_id=active_compaction_run_id,
    )


@router.post(
    "/temp-threads/{temp_thread_id}/context-preview",
    response_model=ContextState,
    status_code=status.HTTP_200_OK,
)
async def preview_temp_context(
    temp_thread_id: str,
    payload: ContextPreviewRequest,
    token_info: Principal = Depends(require_scopes("native:runs:read", "models:read")),
):
    user_id = token_info["user_id"]
    project_id = token_info["project_id"]
    planned = await prepare_context_input(
        user_id=user_id,
        project_id=project_id,
        conversation_id=None,
        temp_thread_id=temp_thread_id,
        model_id=payload.model_id,
        parts=payload.parts,
        features=payload.features,
        agent_id=None,
        skill_ids=payload.skill_ids,
        execution_mode=payload.execution_mode,
        reasoning_effort=payload.reasoning_effort,
        code_workspace_id=None,
        client_timezone=payload.client_timezone,
        token_info=token_info,
        is_preview=True,
        is_context_operation=True,
    )
    from lumen.services import context_manager

    resolved = planned["resolved"]
    capabilities = resolved.get("capabilities") or {}
    context_limit = capabilities.get("context_limit")
    output_reserve = min(planned["max_tokens"], capabilities.get("max_output_tokens", 4096))
    source = planned["source"]
    active_compaction_run_id = await _active_context_compaction_run_id(temp_thread_id=temp_thread_id)

    return context_manager.context_state(
        planned["input_messages"],
        planned["tool_schemas"],
        model_name=resolved["model_name"],
        context_limit=context_limit,
        output_reserve=output_reserve,
        revision=source["revision"],
        checkpoint_id=source.get("checkpoint_id"),
        active_compaction_run_id=active_compaction_run_id,
    )


@router.post(
    "/conversations/{conversation_id}/compactions",
    response_model=ChatRunDescriptor,
    status_code=status.HTTP_202_ACCEPTED,
)
async def compact_conversation(
    conversation_id: str,
    payload: CompactionRequest,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    token_info: Principal = Depends(require_scopes("native:conversations:write", "native:runs:write", "models:read")),
):
    user_id = token_info["user_id"]
    project_id = token_info["project_id"]
    features = _features_payload(payload.features)
    intent = {
        "endpoint": "compaction",
        "conversation_id": conversation_id,
        "model_id": payload.model_id,
        "features": features,
        "reasoning_effort": payload.reasoning_effort,
        "agent_id": payload.agent_id,
        "execution_mode": payload.execution_mode,
        "code_workspace_id": payload.code_workspace_id,
        "skill_ids": payload.skill_ids,
        "client_timezone": payload.client_timezone,
        "expected_context_revision": payload.expected_context_revision,
    }
    intent.update(_api_key_id=token_info["api_key_id"], _source=token_info["source"])
    try:
        existing = await admission.existing_run_for_intent(
            project_id=project_id,
            user_id=user_id,
            client_request_id=str(idempotency_key),
            intent=intent,
            conversation_id=conversation_id,
        )
        if existing is not None:
            return existing
    except durable_errors.DurableRunError as exc:
        raise _run_error(exc) from exc
    except cs.ChatStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        await credit.precheck(user_id, project_id, api_key_id=token_info["api_key_id"])
    except credit.QuotaExceeded as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc
    except credit.ChatStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    planned = await prepare_context_input(
        user_id=user_id,
        project_id=project_id,
        conversation_id=conversation_id,
        temp_thread_id=None,
        model_id=payload.model_id,
        parts=None,
        features=payload.features,
        agent_id=int(payload.agent_id) if payload.agent_id else None,
        skill_ids=payload.skill_ids,
        execution_mode=payload.execution_mode,
        reasoning_effort=payload.reasoning_effort,
        code_workspace_id=payload.code_workspace_id,
        client_timezone=payload.client_timezone,
        token_info=token_info,
        is_preview=False,
        is_context_operation=True,
    )

    from lumen.services import context_manager

    resolved = planned["resolved"]
    capabilities = resolved.get("capabilities") or {}
    context_limit = capabilities.get("context_limit")
    output_reserve = min(planned["max_tokens"], capabilities.get("max_output_tokens", 4096))
    source = planned["source"]

    if source.get("revision") != payload.expected_context_revision:
        raise HTTPException(status_code=409, detail="context_revision_changed")

    state = context_manager.context_state(
        planned["input_messages"],
        planned["tool_schemas"],
        model_name=resolved["model_name"],
        context_limit=context_limit,
        output_reserve=output_reserve,
        revision=source["revision"],
        checkpoint_id=source.get("checkpoint_id"),
    )

    if state.input_budget is None or state.reason_code == "context_budget_unavailable":
        raise HTTPException(status_code=422, detail="context_budget_unavailable")
    if not state.can_compact:
        raise HTTPException(status_code=422, detail="nothing_to_compact")

    from lumen.services.litellm_client import count_context_tokens

    retained, _older = context_manager._required_messages(planned["input_messages"])
    retained_count = count_context_tokens(resolved["model_name"], retained, planned["tool_schemas"]).tokens
    if retained_count is not None and state.input_budget is not None and retained_count > state.input_budget:
        raise HTTPException(status_code=422, detail="context_limit_exceeded")

    execution_protocol_version = _admission_execution_protocol_version()
    try:
        return await admission.create_compaction_run(
            project_id=project_id,
            user_id=user_id,
            client_request_id=str(idempotency_key),
            intent=intent,
            conversation_id=conversation_id,
            temp_thread_id=None,
            expected_context_revision=payload.expected_context_revision,
            model_name=payload.model_id,
            request_payload={
                "input_messages": planned["input_messages"],
                "features": features,
                "skill_snapshot": planned["skill_snapshot"],
                "max_tokens": planned["max_tokens"],
                "temperature": planned["temperature"],
                "reasoning_effort": planned["reasoning_effort"],
                "client_timezone": payload.client_timezone,
                "skill_ids": payload.skill_ids,
                "extension_snapshot": planned["extension_selection"],
                "context_source": planned["source"],
                "tool_schemas": planned["tool_schemas"],
            },
            capability_snapshot=planned["capability_snapshot"],
            pricing_snapshot=planned["pricing_snapshot"],
            execution_protocol_version=execution_protocol_version,
            source=token_info["source"],
            api_key_id=token_info["api_key_id"],
        )
    except durable_errors.DurableRunError as exc:
        raise _run_error(exc) from exc


@router.post(
    "/temp-threads/{temp_thread_id}/compactions",
    response_model=ChatRunDescriptor,
    status_code=status.HTTP_202_ACCEPTED,
)
async def compact_temp_thread(
    temp_thread_id: str,
    payload: CompactionRequest,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    token_info: Principal = Depends(require_scopes("native:runs:write", "models:read")),
):
    user_id = token_info["user_id"]
    project_id = token_info["project_id"]
    features = _features_payload(payload.features)
    intent = {
        "endpoint": "temp_compaction",
        "temp_thread_id": temp_thread_id,
        "model_id": payload.model_id,
        "features": features,
        "reasoning_effort": payload.reasoning_effort,
        "skill_ids": payload.skill_ids,
        "execution_mode": payload.execution_mode,
        "code_workspace_id": payload.code_workspace_id,
        "client_timezone": payload.client_timezone,
        "expected_context_revision": payload.expected_context_revision,
    }
    intent.update(_api_key_id=token_info["api_key_id"], _source=token_info["source"])
    try:
        existing = await admission.existing_run_for_intent(
            project_id=project_id,
            user_id=user_id,
            client_request_id=str(idempotency_key),
            intent=intent,
            conversation_id=None,
        )
        if existing is not None:
            return existing
    except durable_errors.DurableRunError as exc:
        raise _run_error(exc) from exc
    except cs.ChatStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        await credit.precheck(user_id, project_id, api_key_id=token_info["api_key_id"])
    except credit.QuotaExceeded as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc
    except credit.ChatStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    planned = await prepare_context_input(
        user_id=user_id,
        project_id=project_id,
        conversation_id=None,
        temp_thread_id=temp_thread_id,
        model_id=payload.model_id,
        parts=None,
        features=payload.features,
        agent_id=None,
        skill_ids=payload.skill_ids,
        execution_mode=payload.execution_mode,
        reasoning_effort=payload.reasoning_effort,
        code_workspace_id=None,
        client_timezone=payload.client_timezone,
        token_info=token_info,
        is_preview=False,
        is_context_operation=True,
    )

    from lumen.services import context_manager

    resolved = planned["resolved"]
    capabilities = resolved.get("capabilities") or {}
    context_limit = capabilities.get("context_limit")
    output_reserve = min(planned["max_tokens"], capabilities.get("max_output_tokens", 4096))
    source = planned["source"]

    if source.get("revision") != payload.expected_context_revision:
        raise HTTPException(status_code=409, detail="context_revision_changed")

    state = context_manager.context_state(
        planned["input_messages"],
        planned["tool_schemas"],
        model_name=resolved["model_name"],
        context_limit=context_limit,
        output_reserve=output_reserve,
        revision=source["revision"],
        checkpoint_id=source.get("checkpoint_id"),
    )

    if state.input_budget is None or state.reason_code == "context_budget_unavailable":
        raise HTTPException(status_code=422, detail="context_budget_unavailable")
    if not state.can_compact:
        raise HTTPException(status_code=422, detail="nothing_to_compact")

    from lumen.services.litellm_client import count_context_tokens

    retained, _older = context_manager._required_messages(planned["input_messages"])
    retained_count = count_context_tokens(resolved["model_name"], retained, planned["tool_schemas"]).tokens
    if retained_count is not None and state.input_budget is not None and retained_count > state.input_budget:
        raise HTTPException(status_code=422, detail="context_limit_exceeded")

    execution_protocol_version = _admission_execution_protocol_version()
    try:
        return await admission.create_compaction_run(
            project_id=project_id,
            user_id=user_id,
            client_request_id=str(idempotency_key),
            intent=intent,
            conversation_id=None,
            temp_thread_id=temp_thread_id,
            expected_context_revision=payload.expected_context_revision,
            model_name=payload.model_id,
            request_payload={
                "input_messages": planned["input_messages"],
                "features": features,
                "skill_snapshot": planned["skill_snapshot"],
                "max_tokens": planned["max_tokens"],
                "temperature": planned["temperature"],
                "reasoning_effort": planned["reasoning_effort"],
                "client_timezone": payload.client_timezone,
                "skill_ids": payload.skill_ids,
                "extension_snapshot": planned["extension_selection"],
                "context_source": planned["source"],
                "tool_schemas": planned["tool_schemas"],
            },
            capability_snapshot=planned["capability_snapshot"],
            pricing_snapshot=planned["pricing_snapshot"],
            execution_protocol_version=execution_protocol_version,
            source=token_info["source"],
            api_key_id=token_info["api_key_id"],
        )
    except durable_errors.DurableRunError as exc:
        raise _run_error(exc) from exc


def _cursor(last_event_id: str | None, after_seq: int | None, run_id: str) -> int:
    if last_event_id and after_seq is not None:
        try:
            cursor_run, cursor_seq = last_event_id.rsplit(":", 1)
            if cursor_run != run_id or int(cursor_seq) != after_seq:
                raise ValueError
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="SSE cursors disagree") from exc
    if last_event_id:
        try:
            cursor_run, cursor_seq = last_event_id.rsplit(":", 1)
            if cursor_run != run_id:
                raise ValueError
            return int(cursor_seq)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid Last-Event-ID") from exc
    return after_seq or 0


@router.get("/runs/{run_id}/events")
async def run_events(
    run_id: str,
    after_seq: int | None = Query(default=None, ge=0),
    last_event_id: str | None = Header(default=None),
    token_info: Principal = Depends(require_scopes("native:runs:read")),
):
    cursor = _cursor(last_event_id, after_seq, run_id)
    try:
        pending, terminal = await queries.owned_events(
            run_id=run_id,
            project_id=token_info["project_id"],
            user_id=token_info["user_id"],
            after_seq=cursor,
        )
    except durable_errors.DurableRunError as exc:
        raise _run_error(exc) from exc

    async def generate():
        nonlocal cursor, pending, terminal
        last_keepalive_at = monotonic()
        while True:
            for event in pending:
                cursor = event.seq
                data = event.model_dump(mode="json")
                yield f"id: {event.event_id}\nevent: {event.type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            if terminal:
                return
            now = monotonic()
            if now - last_keepalive_at >= 10:
                yield ": keepalive\n\n"
                last_keepalive_at = now
            await asyncio.sleep(0.1)
            try:
                pending, terminal = await queries.owned_events(
                    run_id=run_id,
                    project_id=token_info["project_id"],
                    user_id=token_info["user_id"],
                    after_seq=cursor,
                )
            except durable_errors.DurableRunNotFound:
                return

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.get("/conversations/{conversation_id}/runs")
async def list_conversation_runs(
    conversation_id: str,
    active: bool = Query(default=False),
    token_info: Principal = Depends(require_scopes("native:runs:read")),
):
    await _load_owned_conv(conversation_id, token_info["user_id"], token_info["project_id"])
    descriptors = await queries.active_run_descriptors(
        conversation_id=conversation_id,
        project_id=token_info["project_id"],
        user_id=token_info["user_id"],
    )
    return descriptors if active else descriptors


@router.get("/temp-threads/{temp_thread_id}")
async def get_temp_thread(temp_thread_id: str, token_info: Principal = Depends(require_scopes("native:runs:read"))):
    try:
        return await queries.owned_temp_thread(
            thread_id=temp_thread_id,
            project_id=token_info["project_id"],
            user_id=token_info["user_id"],
        )
    except durable_errors.DurableRunError as exc:
        raise _run_error(exc) from exc


@router.get("/runs")
async def list_active_runs(
    active: bool = Query(default=True),
    token_info: Principal = Depends(require_scopes("native:runs:read")),
):
    """Server-authoritative active-run snapshot for reload/device reconciliation."""

    if not active:
        return []
    return await queries.active_run_descriptors_for_owner(
        project_id=token_info["project_id"],
        user_id=token_info["user_id"],
    )


@router.get("/runs/{run_id}", response_model=ChatRunResponse)
async def get_run(run_id: str, token_info: Principal = Depends(require_scopes("native:runs:read"))):
    try:
        return await queries.owned_run_response(
            run_id=run_id, project_id=token_info["project_id"], user_id=token_info["user_id"]
        )
    except durable_errors.DurableRunError as exc:
        raise _run_error(exc) from exc


@router.post("/runs/{run_id}/approvals/{call_id}")
async def resolve_tool_approval(
    run_id: str,
    call_id: str,
    payload: ToolApprovalDecisionRequest,
    token_info: Principal = Depends(require_scopes("native:runs:write")),
):
    try:
        return await interactions.resolve_tool_approval(
            run_id=run_id,
            call_id=call_id,
            decision=payload.decision,
            project_id=token_info["project_id"],
            user_id=token_info["user_id"],
        )
    except durable_errors.DurableRunError as exc:
        raise _run_error(exc) from exc


@router.post("/runs/{run_id}/interactions/{interaction_id}")
async def resolve_run_interaction(
    run_id: str,
    interaction_id: str,
    payload: RunInteractionResponseRequest,
    token_info: Principal = Depends(require_scopes("native:runs:write")),
):
    try:
        return await interactions.resolve_run_interaction(
            run_id=run_id,
            interaction_id=interaction_id,
            response=payload.response.model_dump(),
            project_id=token_info["project_id"],
            user_id=token_info["user_id"],
        )
    except durable_errors.DurableRunError as exc:
        raise _run_error(exc) from exc


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, token_info: Principal = Depends(require_scopes("native:runs:write"))):
    try:
        return await lifecycle.request_cancelled(
            run_id=run_id, project_id=token_info["project_id"], user_id=token_info["user_id"]
        )
    except durable_errors.DurableRunError as exc:
        raise _run_error(exc) from exc
