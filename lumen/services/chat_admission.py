"""Route-independent native chat admission preparation."""

from __future__ import annotations

import hashlib
import logging
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException

from lumen.auth import Principal, ensure_scopes
from lumen.config import get_settings
from lumen.models.chat_contracts import ChatFeatureOptions, UserInputPart
from lumen.services import agent_store as ags
from lumen.services import conversation_store as cs
from lumen.services import extensions_store as es
from lumen.services import memory_store as ms
from lumen.services import workspace_store as ws
from lumen.services.providers import errors
from lumen.services.providers import routing as ps

_MAX_TOKENS_CAP = 4096
_MAX_MESSAGE_CHARS = 32000

logger = logging.getLogger(__name__)


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
    except errors.ChatStorageUnavailable as exc:
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
    conv: dict, user_id: str, project_id: str, *, include_memory: bool, include_account_memory: bool = True
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
        **({"include_account": False} if not include_account_memory else {}),
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


def _require_native_admission_scopes(
    token_info: Principal,
    features: ChatFeatureOptions,
    *,
    parts: list[UserInputPart],
    execution_mode: str,
    skill_ids: list[int],
    agent: dict | None,
    extension_selection: dict[str, list[dict[str, object]]],
) -> None:
    """Fail closed against materialized native-run defaults; never downgrade a key's request."""
    if token_info["auth_type"] != "api_key":
        return
    if execution_mode != "chat" or any(part.type != "text" for part in parts):
        raise HTTPException(status_code=403, detail="API 키 native run은 text chat만 지원합니다")

    required: set[str] = set()
    if features.memory:
        required.update(("native:memory:read", "native:memory:write"))
    agent_skill_ids = agent.get("skill_ids") if agent else None
    if skill_ids or agent_skill_ids:
        required.add("native:extensions:read")
    selected_extensions = extension_selection["tools"] or extension_selection["mcp"]
    if selected_extensions:
        required.update(("native:extensions:read", "native:tools:execute"))
    if (
        features.tool_policy.mode != "none"
        or features.web_search.enabled
        or features.web_fetch.enabled
        or features.advisor.enabled
    ):
        required.add("native:tools:execute")
    if agent is not None:
        required.add("native:agents:use")
    ensure_scopes(token_info, *required)
