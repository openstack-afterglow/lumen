"""빌트인 AI 채팅 스트리밍 completions (SSE) — 메시지 버전 트리.

흐름: precheck(fail-closed) → 대화 소유권(user_id) → resolve_model 화이트리스트 → 활성 경로 로드 →
user 메시지 저장(parent=active_leaf) → engine.stream → parent 체인으로 메시지 저장 + active_leaf 갱신 + 과금.
재생성은 대상 답변의 턴-시작 user 아래에 새 assistant 형제를 만든다(다른 모델 가능, active_leaf 이동).

⚠️ 과금은 정상 완료·중단 무관하게 정확히 1회. 모델 replay 에서 role=tool 은 제외(orphaned tool 400 방지).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select

from lumen.auth import get_token_info
from lumen.config import get_settings
from lumen.models.chat_contracts import (
    ChatFeatureOptions,
    CompletionRequest,
    ReasoningEffort,
    RegenerateRequest,
    UserInputPart,
    text_projection_from_user_input_parts,
    validate_user_input_parts,
)
from lumen.services import agent_policy, credit, durable_runs, engine, litellm_client
from lumen.services import agent_store as ags
from lumen.services import conversation_store as cs
from lumen.services import extensions_store as es
from lumen.services import memory_store as ms
from lumen.services import provider_store as ps
from lumen.services import workspace_store as ws

logger = logging.getLogger(__name__)
router = APIRouter()

_MAX_TOKENS_CAP = 4096
_MAX_MESSAGE_CHARS = 32000


class TempCompletionRequest(BaseModel):
    """Canonical first-turn temporary completion request."""

    model_config = ConfigDict(protected_namespaces=())

    parts: list[UserInputPart] = Field(min_length=1, max_length=32)
    model_id: str = Field(min_length=1, max_length=190)
    features: ChatFeatureOptions = Field(default_factory=ChatFeatureOptions)
    reasoning_effort: ReasoningEffort = "auto"
    execution_mode: str = Field(default="chat", pattern="^(chat|plan|code)$")
    code_workspace_id: str | None = Field(default=None, max_length=36)
    skill_ids: list[int] = Field(default_factory=list, max_length=100)
    temp_thread_id: str | None = Field(default=None, max_length=36)

    @field_validator("skill_ids")
    @classmethod
    def validate_skill_ids(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value) or any(item < 1 for item in value):
            raise ValueError("skill_ids must be unique positive ids")
        return value

    @model_validator(mode="after")
    def validate_execution_mode(self) -> TempCompletionRequest:
        if self.execution_mode == "chat" and (
            self.code_workspace_id is not None or self.features.tool_policy.workspace_write_mode != "ask"
        ):
            raise ValueError("chat mode cannot select a code workspace or auto-edit")
        if self.execution_mode != "code" and self.features.tool_policy.workspace_write_mode == "auto_edit":
            raise ValueError("auto_edit is only available in code mode")
        return self


class ToolApprovalDecisionRequest(BaseModel):
    decision: Literal["approve", "deny"]


class RunInteractionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    option_ids: list[str] = Field(default_factory=list, max_length=5)
    text: str | None = Field(default=None, max_length=4_000)

    @field_validator("option_ids")
    @classmethod
    def validate_option_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(not option_id for option_id in value):
            raise ValueError("option_ids must be unique non-empty strings")
        return value


class RunInteractionResponseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response: RunInteractionResponse


def _capability_gate(feature: str, resolved: dict) -> dict:
    """Normalize legacy overrides and canonical gates to one fail-closed shape."""
    capabilities = resolved.get("capabilities") or {}
    if feature == "memory":
        # Manual account memory is an existing local capability. Semantic
        # retrieval and automatic extraction remain separately unavailable.
        return {
            "available": True,
            "mode": "native",
            "reason_code": None,
            "pricing_available": True,
        }
    configured_gate = (capabilities.get("feature_gates") or {}).get(feature)
    base_pricing_available = (
        resolved.get("input_price_per_token") is not None and resolved.get("output_price_per_token") is not None
    )
    if configured_gate is not None:
        gate = dict(configured_gate)
    else:
        legacy_available = {
            "text": True,
            "structured_output": bool(capabilities.get("structured_output")),
            "memory": True,
            "image_input": bool(capabilities.get("vision")),
        }.get(feature, False)
        gate = {
            "available": legacy_available,
            "mode": "native" if legacy_available else "none",
            "reason_code": None if legacy_available else "capability_not_configured",
            "pricing_available": base_pricing_available,
        }

    # Text and executor-backed structured output consume the selected model's
    # actual prices. A display capability must never make an unpriced model billable.
    if feature in {"text", "structured_output"}:
        gate["pricing_available"] = base_pricing_available
    return gate


def _has_priced_advisor_route(routes: dict[str, dict[str, Any]] | None) -> bool:
    route = (routes or {}).get("advisor")
    if not isinstance(route, dict):
        return False
    try:
        return all(
            Decimal(str(route[key])).is_finite() and Decimal(str(route[key])) >= 0
            for key in ("input_price_per_token", "output_price_per_token")
        )
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return False


def _require_execution_capability(
    features: ChatFeatureOptions,
    resolved: dict,
    *,
    parts: list[UserInputPart] | None = None,
    feature_routes: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Reject requested features, modalities, and output combinations without a priced route."""
    requested = ["text"]
    if features.response_format.kind != "text":
        requested.append("structured_output")
    if features.web_search.enabled:
        requested.append("web_search")
    if features.web_fetch.enabled:
        requested.append("web_fetch")
    if features.advisor.enabled:
        requested.append("advisor")
    if features.memory:
        requested.append("memory")
    requested.extend(f"{modality}_output" for modality in features.output_modalities if modality != "text")
    if parts:
        requested.extend(f"{part.type}_input" for part in parts if part.type != "text")

    capabilities = resolved.get("capabilities") or {}
    allowed_combinations = capabilities.get("allowed_output_combinations")
    if allowed_combinations and list(features.output_modalities) not in allowed_combinations:
        raise HTTPException(status_code=422, detail="requested chat output combination is not available")
    if features.tool_policy.mode == "none" and (
        features.web_search.enabled or features.web_fetch.enabled or features.advisor.enabled
    ):
        raise HTTPException(status_code=422, detail="requested chat capability requires tool execution")

    for feature in requested:
        if feature == "advisor":
            if not capabilities.get("function_calling") or not _has_priced_advisor_route(feature_routes):
                raise HTTPException(status_code=422, detail="requested chat capability is not available: advisor")
            continue
        gate = _capability_gate(feature, resolved)
        if not gate.get("available") or not gate.get("pricing_available"):
            reason = gate.get("reason_code") or "pricing_unavailable"
            raise HTTPException(
                status_code=422, detail=f"requested chat capability is not available: {feature} ({reason})"
            )

    # Only managed web search/fetch/advisor have an execution path today. Keep
    # advertised future output modalities fail-closed rather than silently omitting them.
    if features.output_modalities != ["text"]:
        raise HTTPException(status_code=422, detail="requested chat capability is not available")


def _feature_route_snapshot(route: dict[str, Any], *, purpose: str) -> dict[str, Any]:
    """Copy only immutable non-secret route identity into the plaintext run snapshot."""
    fields = ("provider_id", "provider_name", "config_version_hash")
    snapshot = {field: route[field] for field in fields}
    if purpose == "advisor":
        snapshot.update({"model_id": route["model_id"], "model_name": route["model_name"]})
    return snapshot


async def _resolve_feature_routes(features: ChatFeatureOptions) -> dict[str, dict[str, Any]]:
    """Resolve only user-selected managed routes after idempotency admission."""
    routes: dict[str, dict[str, Any]] = {}
    if features.web_search.enabled:
        provider_id = features.web_search.provider_id
        if provider_id is None:
            raise HTTPException(status_code=422, detail="web search provider is required")
        search = await ps.get_active_provider_route(provider_id)
        if search is None:
            raise HTTPException(status_code=422, detail="selected web search provider is not available")
        routes["search"] = search
    if features.advisor.enabled:
        model_id = features.advisor.model_id
        if model_id is None:
            raise HTTPException(status_code=422, detail="advisor model is required")
        advisor = await ps.resolve_model_by_id(model_id)
        if advisor is None:
            raise HTTPException(status_code=422, detail="selected advisor model is not available")
        routes["advisor"] = advisor
    return routes


def _run_snapshots(
    resolved: dict,
    features: dict[str, Any],
    *,
    feature_routes: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist only immutable, non-secret execution and pricing inputs for a run."""
    price_metadata = resolved.get("price_metadata")
    costs = price_metadata.get("cost", price_metadata) if isinstance(price_metadata, dict) else {}
    component_keys = (
        "embedding_input_per_token",
        "web_search_request_per_unit",
        "web_search_context_low_per_unit",
        "web_search_context_medium_per_unit",
        "web_search_context_high_per_unit",
        "web_fetch_request_per_unit",
        "web_fetch_context_per_unit",
        "image_per_unit",
        "audio_input_per_second",
        "audio_output_per_second",
        "video_per_second",
        "sandbox_per_second",
    )
    component_prices = {key: str(costs[key]) for key in component_keys if isinstance(costs, dict) and key in costs}
    advisor_route = (feature_routes or {}).get("advisor")
    if isinstance(advisor_route, dict):
        for price_key, route_key in (
            ("advisor_input_price_per_token", "input_price_per_token"),
            ("advisor_output_price_per_token", "output_price_per_token"),
        ):
            if advisor_route.get(route_key) is not None:
                component_prices[price_key] = str(advisor_route[route_key])
    capability_snapshot = {
        "effective_features": features,
        "provider_id": resolved["provider_id"],
        "model_id": resolved["model_id"],
        "provider_name": resolved["provider_name"],
        "model_name": resolved["model_name"],
        "config_version_hash": resolved["config_version_hash"],
        "capabilities": resolved.get("capabilities") or {},
        "feature_routes": {
            purpose: _feature_route_snapshot(route, purpose=purpose)
            for purpose, route in (feature_routes or {}).items()
        },
    }
    pricing_snapshot = {
        "input_price_per_token": str(resolved["input_price_per_token"]),
        "output_price_per_token": str(resolved["output_price_per_token"]),
        "component_prices": component_prices,
        "price_source": resolved.get("price_source"),
        "price_version": resolved.get("price_version"),
        "provider_name": resolved["provider_name"],
        "model_name": resolved["model_name"],
        "margin_multiplier": str(resolved.get("margin_multiplier", "1")),
        "chat_credit_per_usd": str(get_settings().chat_credit_per_usd),
        "rounding_version": "half_even_v1",
    }
    return capability_snapshot, pricing_snapshot


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _model_input(path_messages: list[dict], extra_user: str | None = None) -> list[dict]:
    """활성 경로 메시지 → 모델 입력. role=tool 은 제외(orphaned tool 400 방지)."""
    msgs = [
        {"role": m["role"], "content": m["content"]} for m in path_messages if m.get("content") and m["role"] != "tool"
    ]
    if extra_user is not None:
        msgs.append({"role": "user", "content": extra_user})
    return msgs


def _normalized_origin(url: object) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    return f"https://{parsed.hostname.lower()}{f':{parsed.port}' if parsed.port else ''}"


async def _resolve_extension_selection(
    agent: dict | None,
    features: ChatFeatureOptions,
    *,
    user_id: str,
    project_id: str,
) -> dict[str, list[dict[str, object]]]:
    """Resolve one owner-scoped, active, frozen extension selection before admission."""
    if features.tool_policy.mode == "none":
        return {"tools": [], "mcp": []}

    async def resolve(kind: str, explicit_ids: list[int] | None, agent_key: str) -> list[dict[str, object]]:
        allowed = [int(item) for item in (agent or {}).get(agent_key, []) if isinstance(item, int)]
        if agent is None:
            selected_ids = explicit_ids
        elif explicit_ids is None:
            selected_ids = allowed
        else:
            if not set(explicit_ids) <= set(allowed):
                raise HTTPException(status_code=422, detail=f"selected {kind} is outside the agent allowlist")
            selected_ids = explicit_ids
        if selected_ids == []:
            return []
        try:
            visible = await es.list_for_user(kind, user_id=user_id, project_id=project_id, active_only=True)
            if selected_ids is None:
                selected_ids = [item["id"] for item in visible if isinstance(item.get("id"), int)]
            credential_versions = (
                await es.mcp_credential_versions(selected_ids, user_id=user_id, project_id=project_id)
                if kind == "mcp"
                else {}
            )
        except es.ChatStorageUnavailable as exc:
            if agent is None and explicit_ids is None:
                return []
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        by_id = {item["id"]: item for item in visible if isinstance(item.get("id"), int)}
        selected: list[dict[str, object]] = []
        for item_id in selected_ids:
            item = by_id.get(item_id)
            if item is None:
                raise HTTPException(status_code=404, detail=f"selected {kind} is unavailable")
            selected_item: dict[str, object] = {
                "id": item_id,
                "name": str(item.get("name") or ""),
                "effect": str(item.get("effect") or "external_mutation"),
                "origin": _normalized_origin(item.get("url")),
                "config_fingerprint": es.selection_fingerprint(item),
            }
            if kind == "mcp":
                selected_item["credential_version"] = credential_versions.get(item_id, 0)
            selected.append(selected_item)
        return selected

    tools = await resolve("tool", features.tool_policy.enabled_tool_ids, "tool_ids")
    mcp = await resolve("mcp", features.tool_policy.enabled_mcp_ids, "mcp_ids")
    return {"tools": tools, "mcp": mcp}


def _capability_extension_snapshot(selection: dict[str, list[dict[str, object]]]) -> dict[str, object]:
    selected = [*selection["tools"], *selection["mcp"]]
    return {
        "tool_ids": [item["id"] for item in selection["tools"]],
        "mcp_ids": [item["id"] for item in selection["mcp"]],
        "effects": [item["effect"] for item in selected],
        "origins": [item["origin"] for item in selected if item["origin"]],
        "config_fingerprints": [item["config_fingerprint"] for item in selected],
    }


async def _load_owned_conv(conversation_id: str, user_id: str, project_id: str) -> dict:
    try:
        return await cs.get_conversation(conversation_id, user_id=user_id, project_id=project_id)
    except cs.ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except cs.ConversationForbidden as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except cs.ChatStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


async def _resolve_model(model_name: str) -> dict:
    if not model_name:
        raise HTTPException(status_code=400, detail="모델이 지정되지 않았습니다")
    try:
        resolved = await ps.resolve_model(model_name)
    except ps.ChatStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if resolved is None:
        raise HTTPException(status_code=400, detail=f"허용되지 않은 모델입니다: {model_name}")
    return resolved


def _validated_reasoning_effort(value: str, resolved: dict) -> str:
    effort = value.strip().lower()
    capabilities = resolved.get("capabilities") or {}
    if effort == "auto":
        return effort
    if not capabilities.get("reasoning"):
        raise HTTPException(status_code=422, detail="선택한 모델은 추론 강도를 지원하지 않습니다")
    if effort == "none":
        return effort
    options = capabilities.get("reasoning_options") or []
    supported = next(
        (
            {str(item).strip().lower() for item in option.get("values", [])}
            for option in options
            if isinstance(option, dict) and option.get("type") == "effort"
        ),
        set(),
    )
    if effort not in supported:
        raise HTTPException(status_code=422, detail=f"선택한 모델은 추론 강도 '{effort}'를 지원하지 않습니다")
    return effort


def _validate_tool_reasoning_compatibility(effort: str, resolved: dict, features: ChatFeatureOptions) -> None:
    """Reject explicit reasoning levels the OpenAI GPT-5 Chat Completions tools route cannot execute."""
    if (
        resolved.get("provider_type") == "openai"
        and str(resolved.get("model_name") or "").strip().lower().startswith("gpt-5")
        and features.tool_policy.mode != "none"
        and effort not in {"auto", "none"}
    ):
        raise HTTPException(
            status_code=422,
            detail="선택한 모델은 도구 사용과 명시적 추론 강도를 함께 지원하지 않습니다. "
            "도구를 끄거나 추론 강도를 자동 또는 없음으로 선택하세요.",
        )


async def _resolve_agent(agent_id: int | None, user_id: str, project_id: str) -> dict | None:
    """Resolve only an active caller-owned project agent for an executable run."""
    if agent_id is None:
        return None
    try:
        agent = await ags.get_agent_for_run(agent_id, user_id=user_id, project_id=project_id)
    except ags.ChatStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if agent is None:
        raise HTTPException(status_code=404, detail="에이전트를 찾을 수 없거나 접근 권한이 없습니다")
    return agent


async def _load_skill_snapshot(
    agent: dict | None, payload_skill_ids: list[int], user_id: str, project_id: str
) -> tuple[list[str], list[dict[str, int | str]]]:
    """Resolve each active, authorized skill exactly once and freeze its content provenance."""
    selected_ids: list[int] = []
    for item_id in [*(agent or {}).get("skill_ids", []), *payload_skill_ids]:
        if isinstance(item_id, int) and item_id not in selected_ids:
            selected_ids.append(item_id)
    if not selected_ids:
        return [], []
    try:
        items = await es.list_for_user("skill", user_id=user_id, project_id=project_id, active_only=True)
    except es.ChatStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    by_id = {item["id"]: item for item in items if isinstance(item.get("id"), int)}
    selected: list[dict] = []
    for item_id in selected_ids:
        item = by_id.get(item_id)
        if item is None or not isinstance(item.get("instructions"), str):
            raise HTTPException(status_code=404, detail="selected skill is unavailable")
        selected.append(item)
    return (
        [item["instructions"] for item in selected],
        [
            {
                "id": item["id"],
                "name": str(item.get("name") or ""),
                "content_hash": hashlib.sha256(item["instructions"].encode()).hexdigest(),
            }
            for item in selected
        ],
    )


async def _load_context(
    conv: dict, user_id: str, project_id: str, *, include_memory: bool
) -> tuple[str | None, list[str]]:
    """Load workspace instructions and only the caller's visible memory scopes."""
    workspace_instr = None
    try:
        workspace_instr = await ws.get_instructions_for_run(conv.get("workspace_id"), user_id=user_id)
    except Exception:
        logger.warning("워크스페이스 지침 로드 실패", exc_info=True)
    if not include_memory:
        return workspace_instr, []
    memories = await ms.active_contents_for_run(
        user_id=user_id,
        project_id=project_id,
        workspace_id=conv.get("workspace_id"),
    )
    return workspace_instr, memories


def _apply_context(
    agent: dict | None,
    workspace_instr: str | None,
    memories: list[str],
    input_messages: list[dict],
    temperature,
    max_tokens_req,
    skill_instructions: list[str] | None = None,
):
    """system 선주입 컨텍스트 구성 — 메모리 → 프로젝트 지침 → 스킬 지침 → 에이전트 지침(구체적일수록 뒤).

    런타임 주입일 뿐 chat_messages 에는 저장하지 않는다(활성 경로 불변). params 는 에이전트에서만.
    반환: (messages, temperature, max_tokens_req)
    """
    preamble: list[dict] = []
    if memories:
        joined = "\n".join(f"- {m}" for m in memories)
        preamble.append({"role": "system", "content": f"사용자에 대해 기억할 사실:\n{joined}"})
    if workspace_instr:
        preamble.append({"role": "system", "content": workspace_instr})
    for instr in skill_instructions or []:
        preamble.append({"role": "system", "content": instr})
    if agent and agent.get("instructions"):
        preamble.append({"role": "system", "content": agent["instructions"]})
    if preamble:
        input_messages = [*preamble, *input_messages]
    params = (agent or {}).get("params") or {}
    if temperature is None:
        temperature = params.get("temperature")
    if max_tokens_req is None:
        max_tokens_req = params.get("max_tokens")
    return input_messages, temperature, max_tokens_req


async def _stream_and_persist(
    *,
    conversation_id: str | None,
    project_id: str,
    user_id: str,
    model_name: str,
    resolved: dict,
    input_messages: list[dict],
    start_parent_id: int | None,
    max_tokens: int | None,
    temperature: float | None,
    persist: bool = True,
    reasoning_effort: str | None = None,
    selected_tool_ids: tuple[int, ...] | None = None,
    selected_mcp_ids: tuple[int, ...] | None = None,
):
    """engine.stream 을 소비해 SSE 를 yield 하고, parent 체인으로 메시지 저장 + active_leaf + 과금.

    start_parent_id 는 이 응답 턴의 부모(신규: 방금 저장한 user 메시지 / 재생성: 턴-시작 user).
    persist=False(임시 채팅)면 메시지를 저장하지 않고 과금(usage_logs)만 한다(conversation_id=None).
    """
    parts: list[str] = []
    reasoning_parts: list[str] = []
    final_usage = None
    final_citations: list | None = None
    charged = False
    errored = False
    state = {"last_parent": start_parent_id}
    _do_persist = persist and conversation_id is not None
    event_id = str(uuid.uuid4())
    input_price_per_token = resolved.get("input_price_per_token")
    output_price_per_token = resolved.get("output_price_per_token")
    price_source = resolved.get("price_source")

    async def _save(role: str, content, tool_calls=None, citations=None, reasoning=None, is_leaf: bool = False):
        if not _do_persist:
            return {"id": None}
        msg = await cs.add_message(
            conversation_id,
            role=role,
            content=content,
            tool_calls=tool_calls,
            citations=citations,
            reasoning=reasoning,
            parent_id=state["last_parent"],
            model_name=(model_name if role == "assistant" else None),
            set_leaf=is_leaf,
        )
        state["last_parent"] = msg["id"]
        return msg

    async def _finalize(text: str, pt: int, ct: int, usage_cost: litellm_client.UsageCost):
        if text and _do_persist:
            try:
                await _save(
                    "assistant",
                    text,
                    citations=final_citations,
                    reasoning=("".join(reasoning_parts) or None),
                    is_leaf=True,
                )
            except Exception:
                logger.warning("assistant 메시지 저장 실패 conv=%s", conversation_id, exc_info=True)
        return await credit.apply_usage(
            event_id=event_id,
            user_id=user_id,
            project_id=project_id,
            model_name=model_name,
            provider=resolved.get("provider_name"),
            prompt_tokens=pt,
            completion_tokens=ct,
            usage_cost=usage_cost,
            margin_multiplier=resolved["margin_multiplier"],
            conversation_id=conversation_id,
            source="web",
        )

    # 요청별 effort 우선, 없으면 전역 기본. (litellm_client 가 지원 모델에만 적용)
    reasoning_effort = reasoning_effort or get_settings().chat_reasoning_effort
    try:
        async for ev in engine.stream(
            model=model_name,
            messages=input_messages,
            project_id=project_id,
            user_id=user_id,
            custom_llm_provider=resolved.get("provider_type"),
            api_base=resolved.get("api_base"),
            api_key=resolved.get("api_key"),
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            selected_tool_ids=selected_tool_ids,
            selected_mcp_ids=selected_mcp_ids,
        ):
            etype = ev.get("type")
            if etype == "token":
                parts.append(ev["text"])
                yield _sse({"type": "token", "text": ev["text"]})
            elif etype == "reasoning":
                # 추론(thinking) 델타 — 라이브 중계 + 누적(최종 답변에 저장해 재로딩 시 유지).
                rtext = ev.get("text", "")
                reasoning_parts.append(rtext)
                yield _sse({"type": "reasoning", "text": rtext})
            elif etype == "tool_call":
                yield _sse({"type": "tool_call", "name": ev.get("name")})
            elif etype == "assistant_tool_calls":
                # 이 스텝 텍스트는 이 메시지로 저장 — 최종 답변 parts 에서 제외(중복 방지).
                parts.clear()
                # 시각화용 SSE 중계 — 툴 호출(이름·인자)을 프론트가 카드로 표시. content 는 선행 사유.
                yield _sse(
                    {
                        "type": "tool_calls",
                        "content": ev.get("content"),
                        "calls": [
                            {"id": tc.get("id"), "name": tc.get("name"), "args": tc.get("args")}
                            for tc in (ev.get("tool_calls") or [])
                        ],
                    }
                )
                try:
                    await _save("assistant", ev.get("content"), tool_calls=ev.get("tool_calls"))
                except Exception:
                    logger.warning("assistant tool_calls 저장 실패 conv=%s", conversation_id, exc_info=True)
            elif etype == "tool_result":
                # 시각화용 SSE 중계 — 툴 실행 결과를 프론트가 카드에 채운다.
                yield _sse(
                    {
                        "type": "tool_result",
                        "tool_call_id": ev.get("tool_call_id"),
                        "name": ev.get("name"),
                        "content": ev.get("content"),
                    }
                )
                try:
                    await _save(
                        "tool",
                        ev.get("content"),
                        tool_calls=[{"tool_call_id": ev.get("tool_call_id"), "name": ev.get("name")}],
                    )
                except Exception:
                    logger.warning("tool_result 저장 실패 conv=%s", conversation_id, exc_info=True)
            elif etype == "citations":
                # 출처 — 최종 답변에 저장(_finalize) + 라이브 SSE 중계.
                final_citations = ev.get("items")
                yield _sse({"type": "citations", "items": final_citations})
            elif etype == "usage":
                final_usage = ev.get("usage")
            elif etype == "error":
                errored = True
                yield _sse({"type": "error", "message": ev.get("message", "오류")})

        if not errored:
            text = "".join(parts)
            pt, ct = litellm_client.extract_usage(model_name, input_messages, text, final_usage)
            usage_cost = litellm_client.cost_from_usage(
                model_name,
                pt,
                ct,
                input_price_per_token=input_price_per_token,
                output_price_per_token=output_price_per_token,
                price_source=price_source,
                provider_type=resolved.get("provider_type"),
            )
            credited = await _finalize(text, pt, ct, usage_cost)
            charged = True
            yield _sse({"type": "done", "prompt_tokens": pt, "completion_tokens": ct, "credited_cost": float(credited)})
    finally:
        # 중단(disconnect) 등 비정상 종료 시에도 부분 사용량을 과금(정확히 1회). 모델 하드 실패는 제외.
        if not charged and not errored and parts:
            text = "".join(parts)
            pt, ct = litellm_client.extract_usage(model_name, input_messages, text, final_usage)
            usage_cost = litellm_client.cost_from_usage(
                model_name,
                pt,
                ct,
                input_price_per_token=input_price_per_token,
                output_price_per_token=output_price_per_token,
                price_source=price_source,
                provider_type=resolved.get("provider_type"),
            )
            try:
                await _finalize(text, pt, ct, usage_cost)
            except Exception:
                logger.warning("중단 후 과금 실패 conv=%s", conversation_id, exc_info=True)


def _features_payload(features: ChatFeatureOptions) -> dict:
    return features.model_dump(mode="json", by_alias=True)


def _admission_execution_protocol_version() -> int:
    """Admit only workers that this API deployment can execute safely."""
    version = get_settings().chat_execution_protocol_version
    try:
        durable_runs._require_supported_execution_protocol_version(version)
    except durable_runs.DurableRunInputError as exc:
        raise HTTPException(status_code=503, detail="configured chat execution protocol is not deployed") from exc
    return version


def _run_error(exc: durable_runs.DurableRunError) -> HTTPException:
    if isinstance(exc, durable_runs.DurableRunConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, durable_runs.DurableRunInputError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, durable_runs.DurableRunCursorExpired):
        return HTTPException(status_code=410, detail=str(exc))
    if isinstance(exc, durable_runs.DurableRunNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=503, detail=str(exc))


@router.post("/conversations/{conversation_id}/completions", status_code=status.HTTP_202_ACCEPTED)
async def create_completion(
    conversation_id: str,
    payload: CompletionRequest,
    idempotency_key: str = Header(...),
    token_info: dict = Depends(get_token_info),
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
        existing = await durable_runs.existing_run_for_intent(
            project_id=project_id,
            user_id=user_id,
            client_request_id=idempotency_key,
            intent=intent,
            conversation_id=conversation_id,
        )
        if existing is not None:
            return existing
    except durable_runs.DurableRunError as exc:
        raise _run_error(exc) from exc
    except cs.ChatStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        await credit.precheck(user_id, project_id)
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
    feature_routes = await _resolve_feature_routes(payload.features)
    _require_execution_capability(payload.features, resolved, parts=payload.parts, feature_routes=feature_routes)
    reasoning_effort = _validated_reasoning_effort(payload.reasoning_effort, resolved)
    _validate_tool_reasoning_compatibility(reasoning_effort, resolved, payload.features)
    skill_instructions, skill_snapshot = await _load_skill_snapshot(agent, payload.skill_ids, user_id, project_id)
    input_messages = _model_input(path["messages"], extra_user=message_text)
    workspace_instr, memories = await _load_context(conv, user_id, project_id, include_memory=payload.features.memory)
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
        return await durable_runs.create_persistent_run(
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
        )
    except durable_runs.DurableRunError as exc:
        raise _run_error(exc) from exc


@router.post(
    "/conversations/{conversation_id}/messages/{message_id}/regenerate",
    status_code=status.HTTP_202_ACCEPTED,
)
async def regenerate_message(
    conversation_id: str,
    message_id: int,
    payload: RegenerateRequest,
    idempotency_key: str = Header(...),
    token_info: dict = Depends(get_token_info),
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
    try:
        existing = await durable_runs.existing_run_for_intent(
            project_id=project_id,
            user_id=user_id,
            client_request_id=idempotency_key,
            intent=intent,
            conversation_id=conversation_id,
        )
        if existing is not None:
            return existing
    except durable_runs.DurableRunError as exc:
        raise _run_error(exc) from exc
    execution_protocol_version = _admission_execution_protocol_version()
    resolved = await _resolve_model(payload.model_id)
    feature_routes = await _resolve_feature_routes(payload.features)
    _require_execution_capability(payload.features, resolved, feature_routes=feature_routes)
    reasoning_effort = _validated_reasoning_effort(payload.reasoning_effort, resolved)
    _validate_tool_reasoning_compatibility(reasoning_effort, resolved, payload.features)
    path_messages = await cs.path_ending_at(
        conversation_id, user_id=user_id, project_id=project_id, message_id=turn_user["id"]
    )
    workspace_instr, memories = await _load_context(conv, user_id, project_id, include_memory=payload.features.memory)
    input_messages, temperature, max_tokens = _apply_context(
        None, workspace_instr, memories, _model_input(path_messages), None, None, skill_instructions=[]
    )
    capability_snapshot, pricing_snapshot = _run_snapshots(resolved, features, feature_routes=feature_routes)
    capability_snapshot["execution_protocol_version"] = execution_protocol_version
    try:
        return await durable_runs.create_run(
            project_id=project_id,
            user_id=user_id,
            client_request_id=idempotency_key,
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
        )
    except durable_runs.DurableRunError as exc:
        raise _run_error(exc) from exc


@router.post(
    "/conversations/{conversation_id}/runs/{run_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_failed_run(
    conversation_id: str,
    run_id: str,
    idempotency_key: str = Header(...),
    token_info: dict = Depends(get_token_info),
):
    project_id = token_info["project_id"]
    user_id = token_info["user_id"]
    conv = await _load_owned_conv(conversation_id, user_id, project_id)

    intent = {
        "endpoint": "retry",
        "conversation_id": conversation_id,
        "source_run_id": run_id,
    }
    try:
        existing = await durable_runs.existing_run_for_intent(
            project_id=project_id,
            user_id=user_id,
            client_request_id=idempotency_key,
            intent=intent,
            conversation_id=conversation_id,
        )
        if existing is not None:
            return existing
    except durable_runs.DurableRunError as exc:
        raise _run_error(exc) from exc
    try:
        await credit.precheck(user_id, project_id)
    except credit.QuotaExceeded as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc
    except credit.ChatStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    factory = durable_runs._factory()
    async with factory() as session:
        source_run = (
            (
                await session.execute(
                    select(durable_runs.ChatRun).where(
                        durable_runs.ChatRun.id == run_id,
                        durable_runs.ChatRun.conversation_id == conversation_id,
                        durable_runs.ChatRun.user_id == user_id,
                        durable_runs.ChatRun.project_id == project_id,
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
    feature_routes = await _resolve_feature_routes(features_obj)
    _require_execution_capability(features_obj, resolved, parts=typed_parts, feature_routes=feature_routes)
    reasoning_effort = _validated_reasoning_effort(reasoning_effort, resolved)
    _validate_tool_reasoning_compatibility(reasoning_effort, resolved, features_obj)
    skill_instructions, skill_snapshot = await _load_skill_snapshot(agent, skill_ids, user_id, project_id)

    workspace_instr, memories = await _load_context(conv, user_id, project_id, include_memory=features_obj.memory)
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
        return await durable_runs.create_run(
            project_id=project_id,
            user_id=user_id,
            client_request_id=idempotency_key,
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
        )
    except durable_runs.DurableRunError as exc:
        raise _run_error(exc) from exc


@router.post("/temp-completions", status_code=status.HTTP_202_ACCEPTED)
async def temp_completion(
    payload: TempCompletionRequest,
    idempotency_key: str = Header(...),
    token_info: dict = Depends(get_token_info),
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
    try:
        existing = await durable_runs.existing_run_for_intent(
            project_id=project_id,
            user_id=user_id,
            client_request_id=idempotency_key,
            intent=intent,
            conversation_id=None,
        )
        if existing is not None:
            return existing
        await credit.precheck(user_id, project_id)
    except durable_runs.DurableRunError as exc:
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
        return await durable_runs.create_temp_run(
            project_id=project_id,
            user_id=user_id,
            client_request_id=idempotency_key,
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
        )
    except durable_runs.DurableRunError as exc:
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
    token_info: dict = Depends(get_token_info),
):
    cursor = _cursor(last_event_id, after_seq, run_id)
    try:
        pending, terminal = await durable_runs.owned_events(
            run_id=run_id,
            project_id=token_info["project_id"],
            user_id=token_info["user_id"],
            after_seq=cursor,
        )
    except durable_runs.DurableRunError as exc:
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
                pending, terminal = await durable_runs.owned_events(
                    run_id=run_id,
                    project_id=token_info["project_id"],
                    user_id=token_info["user_id"],
                    after_seq=cursor,
                )
            except durable_runs.DurableRunNotFound:
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
    token_info: dict = Depends(get_token_info),
):
    await _load_owned_conv(conversation_id, token_info["user_id"], token_info["project_id"])
    descriptors = await durable_runs.active_run_descriptors(
        conversation_id=conversation_id,
        project_id=token_info["project_id"],
        user_id=token_info["user_id"],
    )
    return descriptors if active else descriptors


@router.get("/temp-threads/{temp_thread_id}")
async def get_temp_thread(temp_thread_id: str, token_info: dict = Depends(get_token_info)):
    try:
        return await durable_runs.owned_temp_thread(
            thread_id=temp_thread_id,
            project_id=token_info["project_id"],
            user_id=token_info["user_id"],
        )
    except durable_runs.DurableRunError as exc:
        raise _run_error(exc) from exc


@router.get("/runs")
async def list_active_runs(
    active: bool = Query(default=True),
    token_info: dict = Depends(get_token_info),
):
    """Server-authoritative active-run snapshot for reload/device reconciliation."""

    if not active:
        return []
    return await durable_runs.active_run_descriptors_for_owner(
        project_id=token_info["project_id"],
        user_id=token_info["user_id"],
    )


@router.get("/runs/{run_id}")
async def get_run(run_id: str, token_info: dict = Depends(get_token_info)):
    try:
        return await durable_runs.owned_run_response(
            run_id=run_id, project_id=token_info["project_id"], user_id=token_info["user_id"]
        )
    except durable_runs.DurableRunError as exc:
        raise _run_error(exc) from exc


@router.post("/runs/{run_id}/approvals/{call_id}")
async def resolve_tool_approval(
    run_id: str,
    call_id: str,
    payload: ToolApprovalDecisionRequest,
    token_info: dict = Depends(get_token_info),
):
    try:
        return await durable_runs.resolve_tool_approval(
            run_id=run_id,
            call_id=call_id,
            decision=payload.decision,
            project_id=token_info["project_id"],
            user_id=token_info["user_id"],
        )
    except durable_runs.DurableRunError as exc:
        raise _run_error(exc) from exc


@router.post("/runs/{run_id}/interactions/{interaction_id}")
async def resolve_run_interaction(
    run_id: str,
    interaction_id: str,
    payload: RunInteractionResponseRequest,
    token_info: dict = Depends(get_token_info),
):
    try:
        return await durable_runs.resolve_run_interaction(
            run_id=run_id,
            interaction_id=interaction_id,
            response=payload.response.model_dump(),
            project_id=token_info["project_id"],
            user_id=token_info["user_id"],
        )
    except durable_runs.DurableRunError as exc:
        raise _run_error(exc) from exc


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, token_info: dict = Depends(get_token_info)):
    try:
        return await durable_runs.request_cancelled(
            run_id=run_id, project_id=token_info["project_id"], user_id=token_info["user_id"]
        )
    except durable_runs.DurableRunError as exc:
        raise _run_error(exc) from exc
