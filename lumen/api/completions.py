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
    CompletionRequest,
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
    _apply_context,
    _capability_extension_snapshot,
    _load_context,
    _load_owned_conv,
    _load_skill_snapshot,
    _model_input,
    _require_execution_capability,
    _require_native_admission_scopes,
    _resolve_agent,
    _resolve_extension_selection,
    _resolve_feature_routes,
    _resolve_model,
    _run_snapshots,
    _validate_tool_reasoning_compatibility,
    _validated_reasoning_effort,
)
from lumen.services.durable_runs import admission, common, interactions, lifecycle, queries
from lumen.services.durable_runs import errors as durable_errors

logger = logging.getLogger(__name__)
router = APIRouter()

_MAX_TOKENS_CAP = 4096
_MAX_MESSAGE_CHARS = 32000


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

    conv = await _load_owned_conv(conversation_id, user_id, project_id)
    try:
        path = await cs.get_active_path(conversation_id, user_id=user_id, project_id=project_id)
        features = _features_payload(payload.features)
        intent = {
            "endpoint": "completion",
            "conversation_id": conversation_id,
            "parent_id": str(path["active_leaf_id"]) if path["active_leaf_id"] is not None else None,
            "model_id": payload.model_id,
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
    agent = await _resolve_agent(int(payload.agent_id) if payload.agent_id else None, user_id, project_id)
    if payload.execution_mode != "chat":
        raise HTTPException(status_code=422, detail="requested chat execution mode is not available")
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
    resolved = await _resolve_model(payload.model_id)
    extension_selection = await _resolve_extension_selection(
        agent, payload.features, user_id=user_id, project_id=project_id
    )
    _require_native_admission_scopes(
        token_info,
        payload.features,
        parts=payload.parts,
        execution_mode=payload.execution_mode,
        skill_ids=payload.skill_ids,
        agent=agent,
        extension_selection=extension_selection,
    )
    feature_routes = await _resolve_feature_routes(payload.features)
    _require_execution_capability(payload.features, resolved, parts=payload.parts, feature_routes=feature_routes)
    reasoning_effort = _validated_reasoning_effort(payload.reasoning_effort, resolved)
    _validate_tool_reasoning_compatibility(reasoning_effort, resolved, payload.features)
    skill_instructions, skill_snapshot = await _load_skill_snapshot(agent, payload.skill_ids, user_id, project_id)
    input_messages = _model_input(path["messages"], extra_user=message_text)
    workspace_instr, memories = await _load_context(
        conv,
        user_id,
        project_id,
        include_memory=payload.features.memory,
        include_account_memory=token_info["auth_type"] != "api_key",
    )
    input_messages, temperature, max_tokens = _apply_context(
        agent, workspace_instr, memories, input_messages, None, None, skill_instructions=skill_instructions
    )
    capability_snapshot, pricing_snapshot = _run_snapshots(resolved, features, feature_routes=feature_routes)
    if execution_policy is not None and direct_effects is not None:
        capability_snapshot["execution_policy"] = execution_policy.model_dump(mode="json")
        capability_snapshot["direct_effects"] = sorted(direct_effects)
    capability_snapshot["extensions"] = _capability_extension_snapshot(extension_selection)
    capability_snapshot["execution_protocol_version"] = execution_protocol_version
    try:
        return await admission.create_persistent_run(
            project_id=project_id,
            user_id=user_id,
            client_request_id=idempotency_key,
            intent=intent,
            conversation_id=conversation_id,
            model_name=payload.model_id,
            agent_id=agent.get("id") if agent else None,
            user_content=message_text,
            user_parts=[part.model_dump(mode="json", by_alias=True) for part in payload.parts],
            client_timezone=payload.client_timezone,
            request_payload={
                "input_messages": input_messages,
                "input_parts": [part.model_dump(mode="json", by_alias=True) for part in payload.parts],
                "features": features,
                "skill_snapshot": skill_snapshot,
                "max_tokens": min(max_tokens or _MAX_TOKENS_CAP, _MAX_TOKENS_CAP),
                "temperature": temperature,
                "reasoning_effort": reasoning_effort,
                "client_timezone": payload.client_timezone,
                "skill_ids": payload.skill_ids,
                "extension_snapshot": extension_selection,
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
            pricing_snapshot=pricing_snapshot,
            execution_protocol_version=execution_protocol_version,
            source=token_info["source"],
            api_key_id=token_info["api_key_id"],
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
    conv = await _load_owned_conv(conversation_id, user_id, project_id)
    try:
        turn_user = await cs.find_turn_start_user(
            conversation_id, user_id=user_id, project_id=project_id, message_id=message_id
        )
    except (cs.ConversationNotFound, cs.ConversationForbidden) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if turn_user is None:
        raise HTTPException(status_code=400, detail="재생성할 사용자 턴을 찾을 수 없습니다")
    source_parts = (
        validate_user_input_parts(turn_user["parts"])
        if isinstance(turn_user.get("parts"), list) and turn_user["parts"]
        else validate_user_input_parts([{"type": "text", "text": turn_user.get("content") or ""}])
    )
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
        await credit.precheck(user_id, project_id, api_key_id=token_info["api_key_id"])
    except credit.QuotaExceeded as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc
    except credit.ChatStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    execution_protocol_version = _admission_execution_protocol_version()
    resolved = await _resolve_model(payload.model_id)
    feature_routes = await _resolve_feature_routes(payload.features)
    _require_native_admission_scopes(
        token_info,
        payload.features,
        parts=source_parts,
        execution_mode="chat",
        skill_ids=[],
        agent=None,
        extension_selection={"tools": [], "mcp": []},
    )
    _require_execution_capability(payload.features, resolved, feature_routes=feature_routes)
    reasoning_effort = _validated_reasoning_effort(payload.reasoning_effort, resolved)
    _validate_tool_reasoning_compatibility(reasoning_effort, resolved, payload.features)
    path_messages = await cs.path_ending_at(
        conversation_id, user_id=user_id, project_id=project_id, message_id=turn_user["id"]
    )
    workspace_instr, memories = await _load_context(
        conv,
        user_id,
        project_id,
        include_memory=payload.features.memory,
        include_account_memory=token_info["auth_type"] != "api_key",
    )
    input_messages, temperature, max_tokens = _apply_context(
        None, workspace_instr, memories, _model_input(path_messages), None, None, skill_instructions=[]
    )
    capability_snapshot, pricing_snapshot = _run_snapshots(resolved, features, feature_routes=feature_routes)
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
                "input_messages": input_messages,
                "features": features,
                "max_tokens": min(max_tokens or _MAX_TOKENS_CAP, _MAX_TOKENS_CAP),
                "temperature": temperature,
                "reasoning_effort": reasoning_effort,
                "client_timezone": payload.client_timezone,
            },
            capability_snapshot=capability_snapshot,
            pricing_snapshot=pricing_snapshot,
            execution_protocol_version=execution_protocol_version,
            source=token_info["source"],
            api_key_id=token_info["api_key_id"],
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
    conv = await _load_owned_conv(conversation_id, user_id, project_id)

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
        typed_parts = validate_user_input_parts(raw_parts)
    else:
        target_msg = next((m for m in reversed(path_messages) if m["id"] == message_id), None)
        if target_msg and target_msg.get("parts"):
            typed_parts = validate_user_input_parts(target_msg["parts"])
        elif target_msg and target_msg.get("content"):
            typed_parts = validate_user_input_parts([{"type": "text", "text": target_msg["content"]}])
        else:
            typed_parts = validate_user_input_parts([{"type": "text", "text": ""}])
    user_parts = [part.model_dump(mode="json", by_alias=True) for part in typed_parts]
    execution_protocol_version = _admission_execution_protocol_version()
    agent = await _resolve_agent(agent_id, user_id, project_id)
    if agent_id and agent is None:
        raise HTTPException(status_code=404, detail=f"에이전트 {agent_id} 를 찾을 수 없습니다")

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

    resolved = await _resolve_model(model_name)
    extension_selection = await _resolve_extension_selection(
        agent, features_obj, user_id=user_id, project_id=project_id
    )
    _require_native_admission_scopes(
        token_info,
        features_obj,
        parts=typed_parts,
        execution_mode=source_run.execution_mode,
        skill_ids=skill_ids if isinstance(skill_ids, list) else [],
        agent=agent,
        extension_selection=extension_selection,
    )
    feature_routes = await _resolve_feature_routes(features_obj)
    _require_execution_capability(features_obj, resolved, parts=typed_parts, feature_routes=feature_routes)
    reasoning_effort = _validated_reasoning_effort(reasoning_effort, resolved)
    _validate_tool_reasoning_compatibility(reasoning_effort, resolved, features_obj)
    skill_instructions, skill_snapshot = await _load_skill_snapshot(agent, skill_ids, user_id, project_id)

    workspace_instr, memories = await _load_context(
        conv,
        user_id,
        project_id,
        include_memory=features_obj.memory,
        include_account_memory=token_info["auth_type"] != "api_key",
    )
    input_messages, temperature, max_tokens = _apply_context(
        agent, workspace_instr, memories, _model_input(path_messages), None, None, skill_instructions=skill_instructions
    )
    capability_snapshot, pricing_snapshot = _run_snapshots(resolved, features_dict, feature_routes=feature_routes)
    if execution_policy is not None and direct_effects is not None:
        capability_snapshot["execution_policy"] = execution_policy.model_dump(mode="json")
        capability_snapshot["direct_effects"] = sorted(direct_effects)
    capability_snapshot["extensions"] = _capability_extension_snapshot(extension_selection)
    capability_snapshot["execution_protocol_version"] = execution_protocol_version

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
                "input_messages": input_messages,
                "input_parts": user_parts,
                "features": features_dict,
                "skill_snapshot": skill_snapshot,
                "max_tokens": min(max_tokens or _MAX_TOKENS_CAP, _MAX_TOKENS_CAP),
                "temperature": temperature,
                "reasoning_effort": reasoning_effort,
                "skill_ids": skill_ids,
                "extension_snapshot": extension_selection,
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
            pricing_snapshot=pricing_snapshot,
            execution_protocol_version=execution_protocol_version,
            source=token_info["source"],
            api_key_id=token_info["api_key_id"],
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
        message_text = text_projection_from_user_input_parts(payload.parts)
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
    resolved = await _resolve_model(payload.model_id)
    if payload.execution_mode != "chat":
        raise HTTPException(status_code=422, detail="requested chat execution mode is not available")
    feature_routes = await _resolve_feature_routes(payload.features)
    extension_selection = await _resolve_extension_selection(
        None, payload.features, user_id=user_id, project_id=project_id
    )
    _require_native_admission_scopes(
        token_info,
        payload.features,
        parts=payload.parts,
        execution_mode=payload.execution_mode,
        skill_ids=payload.skill_ids,
        agent=None,
        extension_selection=extension_selection,
    )
    _require_execution_capability(payload.features, resolved, parts=payload.parts, feature_routes=feature_routes)
    reasoning_effort = _validated_reasoning_effort(payload.reasoning_effort, resolved)
    _validate_tool_reasoning_compatibility(reasoning_effort, resolved, payload.features)
    capability_snapshot, pricing_snapshot = _run_snapshots(resolved, features, feature_routes=feature_routes)
    capability_snapshot["extensions"] = _capability_extension_snapshot(extension_selection)
    capability_snapshot["execution_protocol_version"] = execution_protocol_version
    skill_instructions, skill_snapshot = await _load_skill_snapshot(None, payload.skill_ids, user_id, project_id)
    input_messages, temperature, max_tokens = _apply_context(
        None,
        None,
        [],
        [{"role": "user", "content": message_text}],
        None,
        None,
        skill_instructions=skill_instructions,
    )
    try:
        return await admission.create_temp_run(
            project_id=project_id,
            user_id=user_id,
            client_request_id=str(idempotency_key),
            intent=intent,
            temp_thread_id=payload.temp_thread_id,
            model_name=payload.model_id,
            request_payload={
                "input_messages": input_messages,
                "input_parts": [part.model_dump(mode="json", by_alias=True) for part in payload.parts],
                "features": features,
                "max_tokens": min(max_tokens or _MAX_TOKENS_CAP, _MAX_TOKENS_CAP),
                "temperature": temperature,
                "reasoning_effort": reasoning_effort,
                "skill_snapshot": skill_snapshot,
                "skill_ids": payload.skill_ids,
                "extension_snapshot": extension_selection,
            },
            capability_snapshot=capability_snapshot,
            pricing_snapshot=pricing_snapshot,
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
        while True:
            for event in pending:
                cursor = event.seq
                data = event.model_dump(mode="json")
                yield f"id: {event.event_id}\nevent: {event.type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            if terminal:
                return
            yield ": keepalive\n\n"
            await asyncio.sleep(1)
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
