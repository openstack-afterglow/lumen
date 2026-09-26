"""Route-independent native chat admission preparation."""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException
from lumen_plugin_api.contracts import PluginError

from lumen.auth import Principal, ensure_scopes
from lumen.config import get_settings
from lumen.models.chat_contracts import (
    AgentBudget,
    ChatFeatureOptions,
    UserInputPart,
    text_projection_from_user_input_parts,
)
from lumen.plugins import bindings as plugin_bindings
from lumen.plugins import memory_host, skills_host
from lumen.plugins.registry import get_registry
from lumen.services import agent_store as ags
from lumen.services import context_inspector as inspector
from lumen.services import conversation_store as cs
from lumen.services import extensions_store as es
from lumen.services import memory_store as ms
from lumen.services import workspace_store as ws
from lumen.services.capabilities import reasoning_can_be_disabled
from lumen.services.providers import errors
from lumen.services.providers import routing as ps
from lumen.services.providers.pricing import _has_component_prices
from lumen.services.tool_runtime import contracts

_MAX_TOKENS_CAP = 4096
_MAX_MESSAGE_CHARS = 32000
_LOAD_POLICIES = ("preloaded", "on_demand")

logger = logging.getLogger(__name__)


def _plugin_http(exc: PluginError) -> HTTPException:
    """503 when the required runtime is unavailable; 422 when the selection itself is invalid."""
    status = 503 if exc.code in {"plugin_unavailable", "plugin_incompatible"} else 422
    return HTTPException(status_code=status, detail=exc.code)


def _resolve_plugin_ids(agent: dict | None, explicit_ids: list[str], *, kind: str, union: bool) -> list[str]:
    """Skills union the agent and request; tools narrow the agent allowlist like HTTP tools."""
    allowed = [item for item in (agent or {}).get(f"plugin_{kind}_ids", []) if isinstance(item, str)]
    if union:
        return list(dict.fromkeys([*allowed, *explicit_ids]))
    if agent is None:
        return list(explicit_ids)
    if not explicit_ids:
        return allowed
    if not set(explicit_ids) <= set(allowed):
        raise HTTPException(status_code=422, detail=f"selected plugin {kind} is outside the agent allowlist")
    return list(explicit_ids)


async def _freeze_plugin_bindings(ids: list[str], *, kind: str, user_id: str, project_id: str) -> list[dict]:
    if not ids:
        return []
    try:
        return await plugin_bindings.freeze_bindings(ids, kind=kind, user_id=user_id, project_id=project_id)
    except PluginError as exc:
        raise _plugin_http(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="plugin binding selection is invalid") from exc
    except es.ChatStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

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
        (features.web_search.enabled and features.web_search.mode == "managed")
        or features.web_fetch.enabled
        or features.advisor.enabled
    ):
        raise HTTPException(status_code=422, detail="requested chat capability requires tool execution")

    for feature in requested:
        if feature == "advisor":
            if not capabilities.get("function_calling") or not _has_priced_advisor_route(feature_routes):
                raise HTTPException(status_code=422, detail="requested chat capability is not available: advisor")
            continue
        gate = _capability_gate(feature, resolved)
        if feature == "web_search" and features.web_search.mode == "native" and gate.get("mode") != "native":
            raise HTTPException(
                status_code=422,
                detail="requested chat capability is not available: web_search (provider_unsupported)",
            )
        if feature == "web_search" and features.web_search.mode == "managed" and gate.get("mode") == "native":
            # A native model's token price is not the managed search price.
            # Preserve admission rejection before any chargeable tool work.
            metadata = resolved.get("price_metadata") or {}
            costs = metadata.get("cost", metadata) if isinstance(metadata, dict) else {}
            if not isinstance(costs, dict) or not _has_component_prices(
                costs,
                "web_search_request_per_unit",
                "web_search_context_low_per_unit",
                "web_search_context_medium_per_unit",
                "web_search_context_high_per_unit",
            ):
                gate.update(pricing_available=False, reason_code="pricing_unavailable")
        if not gate.get("available") or not gate.get("pricing_available"):
            reason = gate.get("reason_code") or "pricing_unavailable"
            raise HTTPException(
                status_code=422, detail=f"requested chat capability is not available: {feature} ({reason})"
            )

    # Output generation modalities still lack execution paths. Keep them
    # fail-closed rather than silently omitting an advertised modality.
    if features.output_modalities != ["text"]:
        raise HTTPException(status_code=422, detail="requested chat capability is not available")


_CACHE_PRICE_KEYS = (
    "cache_read_price_per_token",
    "cache_write_price_per_token",
    "cache_write_1h_price_per_token",
    "cache_read_price_per_token_above_200k",
    "cache_write_price_per_token_above_200k",
    "cache_write_1h_price_per_token_above_200k",
)


def _cache_price_snapshot(route: dict[str, Any]) -> dict[str, Any]:
    """Freeze resolved direct-provider or manual cache prices and provenance."""
    return {
        **{key: str(route[key]) if route.get(key) is not None else None for key in _CACHE_PRICE_KEYS},
        "cache_price_sources": route.get("cache_price_sources") or {},
    }


def _feature_route_snapshot(route: dict[str, Any], *, purpose: str) -> dict[str, Any]:
    """Copy only immutable non-secret route identity into the plaintext run snapshot."""
    fields = ("provider_id", "provider_name", "config_version_hash")
    snapshot = {field: route[field] for field in fields}
    if purpose in {"advisor", "summary"}:
        snapshot.update({"model_id": route["model_id"], "model_name": route["model_name"]})
    if purpose == "advisor":
        capabilities = route.get("capabilities")
        snapshot["context_limit"] = route.get("context_limit") or (
            capabilities.get("context_limit") if isinstance(capabilities, dict) else None
        )
    if purpose == "summary":
        capabilities = route.get("capabilities")
        context_limit = route.get("context_limit")
        if context_limit is None and isinstance(capabilities, dict):
            context_limit = capabilities.get("context_limit")
        snapshot["context_limit"] = context_limit
    return snapshot


async def resolve_summary_route(execution_route: dict[str, Any]) -> dict[str, Any]:
    """Resolve the dedicated title route once, falling back to the execution route."""
    try:
        route = await ps.resolve_title_model()
    except errors.ChatStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return route or execution_route


async def _resolve_feature_routes(features: ChatFeatureOptions) -> dict[str, dict[str, Any]]:
    """Resolve only user-selected managed routes after idempotency admission."""
    routes: dict[str, dict[str, Any]] = {}
    if features.web_search.enabled and features.web_search.mode == "managed":
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
    summary_route: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist immutable execution, summary-route, and pricing inputs for a run."""
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
            # Advisor prices use the same resolved manual or direct-provider rates.
            ("advisor_cache_read_price_per_token", "cache_read_price_per_token"),
            ("advisor_cache_write_price_per_token", "cache_write_price_per_token"),
            ("advisor_cache_write_1h_price_per_token", "cache_write_1h_price_per_token"),
            ("advisor_cache_read_price_per_token_above_200k", "cache_read_price_per_token_above_200k"),
            ("advisor_cache_write_price_per_token_above_200k", "cache_write_price_per_token_above_200k"),
            ("advisor_cache_write_1h_price_per_token_above_200k", "cache_write_1h_price_per_token_above_200k"),
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
        **_cache_price_snapshot(resolved),
    }
    if summary_route is not None:
        capability_snapshot["summary_route"] = _feature_route_snapshot(summary_route, purpose="summary")
        pricing_snapshot["summary_route"] = {
            "input_price_per_token": str(summary_route.get("input_price_per_token"))
            if summary_route.get("input_price_per_token") is not None
            else None,
            "output_price_per_token": str(summary_route.get("output_price_per_token"))
            if summary_route.get("output_price_per_token") is not None
            else None,
            "price_source": summary_route.get("price_source"),
            "price_version": summary_route.get("price_version"),
            "provider_name": summary_route.get("provider_name"),
            "model_name": summary_route.get("model_name"),
            **_cache_price_snapshot(summary_route),
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
        # A selection nobody spelled out is "whatever is visible", which now
        # includes administrator-installed connector bundles. An unconnected
        # OAuth connector must not be frozen into such a selection.
        implicit_selection = selected_ids is None
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
                # Runtime exposes preloaded extensions in the first provider request and
                # keeps on_demand ones behind the tool catalog, so context planning must
                # know the policy without re-reading the extension row.
                "load_policy": item.get("load_policy") if item.get("load_policy") in _LOAD_POLICIES else "on_demand",
                # Bindings exclude any extension without a canonical destination, so
                # context planning must not count or offer one either.
                "destination_origin": contracts._v2_destination_origin(item.get("url")),
            }
            if kind == "tool":
                selected_item["description"] = str(item.get("description") or item.get("name") or "Custom HTTP tool")
                selected_item["params_schema"] = item.get("params_schema")
            if kind == "mcp":
                # ``None`` here means exactly one thing: an OAuth-gated server
                # with no usable connection for this owner. Freezing it would
                # make the worker's re-validation reject it and surface a
                # "credential changed or was revoked" warning on every single
                # run, which is wrong for a connector the caller never chose.
                # An explicitly selected one still warns — that request asked
                # for a server it cannot reach.
                credential_version = credential_versions.get(item_id, 0)
                if credential_version is None and implicit_selection:
                    continue
                selected_item["credential_version"] = credential_version
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
        if not reasoning_can_be_disabled(capabilities, resolved.get("provider_type")):
            raise HTTPException(
                status_code=422,
                detail="선택한 모델은 추론 끄기('none')를 지원하지 않습니다. 자동 또는 모델이 지원하는 추론 강도를 선택하세요",
            )
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
    """Reject explicit reasoning levels the OpenAI GPT-5 Chat Completions tools route cannot execute.

    ``none`` reaches this check only after ``_validated_reasoning_effort`` confirmed the model
    advertises it, so it stays allowed here.
    """
    if (
        resolved.get("provider_type") == "openai"
        and str(resolved.get("model_name") or "").strip().lower().startswith("gpt-5")
        and features.tool_policy.mode != "none"
        and effort not in {"auto", "none"}
    ):
        can_disable = reasoning_can_be_disabled(resolved.get("capabilities"), resolved.get("provider_type"))
        choices = "자동 또는 없음" if can_disable else "자동"
        raise HTTPException(
            status_code=422,
            detail="선택한 모델은 도구 사용과 명시적 추론 강도를 함께 지원하지 않습니다. "
            f"도구를 끄거나 추론 강도를 {choices}으로 선택하세요.",
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
    agent: dict | None,
    payload_skill_ids: list[int],
    plugin_skill_snapshots: list[dict],
    user_id: str,
    project_id: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Resolve each authorized skill exactly once through the selected provider and freeze it."""
    selected_ids: list[int] = []
    for item_id in [*(agent or {}).get("skill_ids", []), *payload_skill_ids]:
        if isinstance(item_id, int) and item_id not in selected_ids:
            selected_ids.append(item_id)
    if not selected_ids and not plugin_skill_snapshots:
        return [], []
    try:
        instructions, frozen = await skills_host.resolve_skills(
            selected_ids, plugin_skill_snapshots, user_id=user_id, project_id=project_id
        )
    except PluginError as exc:
        if exc.code == "plugin_authority_revoked":
            raise HTTPException(status_code=404, detail="selected skill is unavailable") from exc
        raise _plugin_http(exc) from exc
    snapshot: list[dict[str, Any]] = []
    for item in frozen:
        public = item["snapshot"]
        reference = public["reference"]
        snapshot.append(
            {
                **item,
                "id": reference.get("database_id") or reference.get("binding_id"),
                "name": public.get("name") or (item.get("binding") or {}).get("binding", {}).get("name") or "",
                "content_hash": public["content_digest"],
            }
        )
    return instructions, snapshot


async def _load_context(
    conv: dict,
    user_id: str,
    project_id: str,
    *,
    include_memory: bool,
    include_account_memory: bool = True,
    memory_strategy: str = "recency",
    memory_query: str = "",
) -> tuple[str | None, list[str]]:
    """Load workspace instructions and only the caller's visible memory scopes."""
    workspace_instr = None
    try:
        workspace_instr = await ws.get_instructions_for_run(conv.get("workspace_id"), user_id=user_id)
    except Exception:
        logger.warning("워크스페이스 지침 로드 실패", exc_info=True)
    if not include_memory:
        return workspace_instr, []
    settings = get_settings()
    semantic = memory_strategy == "semantic"
    try:
        memories = await memory_host.recall_for_run(
            user_id=user_id,
            project_id=project_id,
            workspace_id=conv.get("workspace_id"),
            include_account=include_account_memory,
            strategy=memory_strategy,
            query=memory_query if semantic else "",
            limit=min(30, max(1, settings.chat_memory_candidate_limit)) if semantic else 30,
            token_budget=max(0, settings.chat_memory_retrieval_token_budget) if semantic else 0,
        )
    except PluginError as exc:
        if semantic and exc.code == "plugin_unavailable":
            raise HTTPException(
                status_code=422,
                detail="requested chat capability is not available: memory (semantic_unavailable)",
            ) from exc
        raise _plugin_http(exc) from exc
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
    plugin_tool_snapshots: list[dict],
    plugin_skill_snapshots: list[dict],
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
    if skill_ids or agent_skill_ids or plugin_skill_snapshots:
        required.add("native:extensions:read")
    if plugin_tool_snapshots:
        required.update(("native:extensions:read", "native:tools:execute"))
    selected_extensions = extension_selection["tools"] or extension_selection["mcp"]
    if selected_extensions:
        required.update(("native:extensions:read", "native:tools:execute"))
    if (
        features.tool_policy.mode != "none"
        or (features.web_search.enabled and features.web_search.mode == "managed")
        or features.web_fetch.enabled
        or features.advisor.enabled
    ):
        required.add("native:tools:execute")
    if agent is not None:
        required.add("native:agents:use")
    ensure_scopes(token_info, *required)


def _require_context_read_scopes(
    token_info: Principal,
    features: ChatFeatureOptions,
    *,
    skill_ids: list[int],
    agent: dict | None,
    plugin_skill_snapshots: list[dict],
) -> None:
    """Check read-only context admission scopes without requiring execution or write grants."""
    if token_info["auth_type"] != "api_key":
        return
    required: set[str] = set()
    if features.tool_policy.mode != "none":
        required.add("native:extensions:read")
    if features.memory:
        required.add("native:memory:read")
    agent_skill_ids = agent.get("skill_ids") if agent else None
    if skill_ids or agent_skill_ids or plugin_skill_snapshots:
        required.add("native:extensions:read")
    if agent is not None:
        required.add("native:agents:use")
    ensure_scopes(token_info, *required)


def _bindable(item: dict[str, object]) -> bool:
    """Return whether bindings would expose this extension at all."""
    return isinstance(item.get("id"), int) and bool(item.get("destination_origin"))


def _preloaded(item: dict[str, object]) -> bool:
    return _bindable(item) and item.get("load_policy") != "on_demand"


def _deferred(item: dict[str, object]) -> bool:
    return _bindable(item) and item.get("load_policy") == "on_demand"

async def custom_tool_schema(item: dict[str, object], *, user_id: str, project_id: str) -> dict[str, Any] | None:
    """Project one selected custom tool exactly as bindings would, or ``None`` when bindings would drop it."""
    from lumen.services.tools import ToolContext

    try:
        function = await contracts.custom_tool_function_schema(item, ToolContext(project_id=project_id, user_id=user_id))
    except PluginError as exc:
        if exc.code in {"plugin_unavailable", "plugin_incompatible"}:
            raise _plugin_http(exc) from exc
        return None
    except (TypeError, ValueError):
        return None
    return {"type": "function", "function": function}


def _managed_tool_names(features: ChatFeatureOptions) -> list[str]:
    """Name the managed tools this selection asked for.

    ``graph.stream`` always creates a ``ToolBindingSession``, so neither the v1
    nor the v2 binding path emits managed schemas in the provider request today.
    They are therefore reported as not-included material instead of being
    counted as if the model could call them.
    """
    from lumen.services.tool_runtime import managed

    names: list[str] = []
    if features.web_search.enabled and features.web_search.mode == "managed":
        names.append(managed._MANAGED_SEARCH_TOOL)
    if features.web_fetch.enabled:
        names.append(managed._MANAGED_FETCH_TOOL)
    if features.advisor.enabled:
        names.append(managed._MANAGED_ADVISOR_TOOL)
    return names


async def _preview_tool_schemas(
    features: ChatFeatureOptions,
    extension_selection: dict[str, list[dict[str, object]]],
    *,
    user_id: str,
    project_id: str,
    plugin_tool_snapshots: list[dict] | None = None,
) -> list[dict[str, Any]]:
    """Project exactly the schemas the first provider request will carry.

    The executor sends built-in tools, the on-demand tool catalog, and only
    ``preloaded`` extensions.  ``on_demand`` extensions stay behind
    ``list_available_tools`` and MCP schemas are discovered remotely, so preview
    counts neither instead of inflating the budget with material the model will
    not receive.
    """
    if features.tool_policy.mode == "none":
        return []
    from lumen.services import tools
    from lumen.services.tool_runtime import bindings

    schemas = list(await tools.tool_schemas())
    catalog = bindings._catalog_binding({}, include_managed=False).definition
    schemas.append(
        {
            "type": "function",
            "function": {
                "name": catalog.name,
                "description": catalog.description,
                "parameters": catalog.input_schema,
            },
        }
    )
    for tool in extension_selection.get("tools", []):
        if not _preloaded(tool):
            continue
        schema = await custom_tool_schema(tool, user_id=user_id, project_id=project_id)
        if schema is not None:
            schemas.append(schema)
    for snapshot in plugin_tool_snapshots or []:
        definition = snapshot.get("definition")
        if isinstance(definition, dict):
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": definition["name"],
                        "description": definition["description"],
                        "parameters": definition["input_schema"],
                    },
                }
            )
    return schemas


def _attachment_label(part: UserInputPart) -> str:
    """Name one non-text draft part without exposing its content."""
    identity = getattr(part, "name", None) or getattr(part, "asset_id", None) or ""
    return f"{part.type}:{identity}" if identity else str(part.type)


def _custom_tool_identity(item: dict[str, object]) -> str:
    """Return the provider tool name bindings would expose for this custom tool."""
    return contracts._v2_provider_name("custom", int(item["id"]), item.get("name"))


def _mcp_tool_prefix(item: dict[str, object]) -> str:
    """Return the provider name prefix shared by every tool of this MCP server."""
    return f"mcp__{item['id']}__"


def _context_plan(
    features: ChatFeatureOptions,
    extension_selection: dict[str, list[dict[str, object]]],
    *,
    memories: list[str],
    workspace_instr: str | None,
    workspace_id: object,
    skill_snapshot: list[dict[str, int | str]],
    agent: dict | None,
    parts: list[UserInputPart],
    source: dict[str, Any],
    message_count: int,
) -> dict[str, Any]:
    """Record how this request was assembled, using only bounded safe names.

    The entries mirror ``_apply_context`` exactly: one memory message, one
    workspace message, one message per skill, then the agent instruction.  Each
    entry consumes that many leading instruction messages, so the inspector can
    attribute tokens without inspecting prompt text.
    """
    instructions: list[dict[str, Any]] = []
    if memories:
        instructions.append(inspector.instruction_plan_entry("memory", slots=1, count=len(memories), items=[]))
    if workspace_instr:
        instructions.append(
            inspector.instruction_plan_entry(
                "workspace",
                slots=1,
                count=1,
                items=[f"workspace:{workspace_id}"] if workspace_id else [],
            )
        )
    if skill_snapshot:
        instructions.append(
            inspector.instruction_plan_entry(
                "skills",
                slots=len(skill_snapshot),
                count=len(skill_snapshot),
                items=[item.get("name") or f"skill:{item.get('id')}" for item in skill_snapshot],
            )
        )
    if agent and agent.get("instructions"):
        instructions.append(
            inspector.instruction_plan_entry(
                "agent",
                slots=1,
                count=1,
                items=[agent.get("name") or f"agent:{agent.get('id')}"],
            )
        )
    checkpoint = source.get("checkpoint")
    tools_enabled = features.tool_policy.mode != "none"
    deferred = (
        [
            *(
                inspector.tool_plan_entry(item.get("name") or item.get("id"), _custom_tool_identity(item))
                for item in extension_selection.get("tools", [])
                if _deferred(item)
            ),
            *(
                inspector.tool_plan_entry(f"mcp:{item.get('name') or item.get('id')}", _mcp_tool_prefix(item))
                for item in extension_selection.get("mcp", [])
                if _deferred(item)
            ),
            *(inspector.tool_plan_entry(name, name) for name in _managed_tool_names(features)),
        ]
        if tools_enabled
        else []
    )
    return inspector.build_plan(
        instructions=instructions,
        summary_checkpoint_id=(checkpoint or {}).get("id") if isinstance(checkpoint, dict) else None,
        message_count=message_count,
        attachments=[_attachment_label(part) for part in parts if part.type != "text"],
        deferred_tools=deferred,
        undiscovered_mcp=[
            inspector.tool_plan_entry(f"mcp:{item.get('name') or item.get('id')}", _mcp_tool_prefix(item))
            for item in extension_selection.get("mcp", [])
            if tools_enabled and _preloaded(item)
        ],
    )


async def _project_agent_quota(project_id: str) -> dict[str, Any]:
    """Read the project's finite agent caps; a missing row means delegation/code are disabled."""
    from sqlalchemy import select

    from lumen.db import get_session_factory, is_db_available
    from lumen.models.chat_infrastructure import ChatProjectAgentQuota

    factory = get_session_factory()
    if factory is None or not is_db_available():
        raise HTTPException(status_code=503, detail="chat storage is unavailable")
    async with factory() as session:
        row = (
            await session.execute(select(ChatProjectAgentQuota).where(ChatProjectAgentQuota.project_id == project_id))
        ).scalar_one_or_none()
    defaults = get_settings().runtime_config.project_quota_defaults
    if row is None:
        return {
            "max_active_children": defaults.max_active_children,
            "max_active_sandboxes": defaults.max_active_sandboxes,
            "max_sandbox_seconds": defaults.max_sandbox_seconds,
            "max_credit_reservation": Decimal(defaults.max_credit_reservation),
        }
    return {
        "max_active_children": int(row.max_active_children),
        "max_active_sandboxes": int(row.max_active_sandboxes),
        "max_sandbox_seconds": int(row.max_sandbox_seconds),
        "max_credit_reservation": Decimal(str(row.max_credit_reservation)),
    }


def _agent_runtime_prerequisites(execution_mode: str) -> None:
    """`plan`/`code` need protocol v2 + checkpointer; `code` additionally needs an enabled sandbox pool."""
    from lumen.services.checkpointer import chat_checkpointer

    settings = get_settings()
    if settings.chat_execution_protocol_version != 2 or not chat_checkpointer.available:
        raise HTTPException(
            status_code=422, detail="requested chat execution mode is not available (protocol_v2_required)"
        )
    runtime = settings.runtime_config
    if execution_mode == "code" and not (
        runtime.enabled and any(pool.enabled and pool.role == "sandbox" for pool in runtime.pools)
    ):
        raise HTTPException(status_code=422, detail="requested chat execution mode is not available (sandbox_unavailable)")


async def _validate_agent_budget(
    agent_budget: AgentBudget | None, *, execution_mode: str, project_id: str, agent: dict | None
) -> dict[str, Any] | None:
    """Budgets are explicit, positive and inside the project caps; NULL never means unlimited."""
    delegation_capable = bool(agent and (agent.get("execution_policy") or {}).get("can_delegate_read"))
    delegation_capable = delegation_capable or bool(
        agent and (agent.get("execution_policy") or {}).get("can_delegate_write")
    )
    if agent_budget is None:
        if execution_mode != "chat":
            raise HTTPException(status_code=422, detail="agent_budget is required for plan and code modes")
        return None
    caps = await _project_agent_quota(project_id)
    credit = Decimal(agent_budget.credit_ceiling)
    if caps["max_credit_reservation"] <= 0 or caps["max_sandbox_seconds"] <= 0:
        raise HTTPException(status_code=422, detail="agent execution is disabled for this project (delegation_disabled)")
    if credit > caps["max_credit_reservation"] or agent_budget.sandbox_seconds_ceiling > caps["max_sandbox_seconds"]:
        raise HTTPException(status_code=422, detail="agent_budget exceeds the project caps")
    if execution_mode == "chat" and not delegation_capable:
        # A budget without any delegation- or sandbox-capable path is not an error, but it
        # must not be silently interpreted as enabling either.
        pass
    return {
        "credit_ceiling": format(credit, "f"),
        "sandbox_seconds_ceiling": agent_budget.sandbox_seconds_ceiling,
        "wall_time_seconds": agent_budget.wall_time_seconds,
    }


async def prepare_context_input(
    *,
    user_id: str,
    project_id: str,
    conversation_id: str | None = None,
    temp_thread_id: str | None = None,
    leaf_id: int | None = None,
    model_id: str,
    parts: list[UserInputPart] | None = None,
    features: ChatFeatureOptions,
    agent_id: int | None = None,
    skill_ids: list[int] | None = None,
    plugin_tool_ids: list[str] | None = None,
    plugin_skill_ids: list[str] | None = None,
    agent_budget: AgentBudget | None = None,
    execution_mode: str = "chat",
    reasoning_effort: str = "auto",
    code_workspace_id: str | None = None,
    client_timezone: str | None = None,
    token_info: Principal | None = None,
    is_preview: bool = False,
    is_context_operation: bool = False,
    allow_empty_source: bool = False,
    append_draft: bool = True,
) -> dict[str, Any]:
    from lumen.services import context_store

    skill_ids = skill_ids or []
    plugin_tool_ids = plugin_tool_ids or []
    plugin_skill_ids = plugin_skill_ids or []
    parts = parts or []
    if temp_thread_id is not None:
        if agent_id is not None or code_workspace_id is not None:
            raise HTTPException(status_code=422, detail="temporary chats do not support agents or code workspaces")
        if execution_mode != "chat":
            raise HTTPException(status_code=422, detail="requested chat execution mode is not available")
    if execution_mode != "chat":
        _agent_runtime_prerequisites(execution_mode)

    conv = None
    if conversation_id is not None:
        conv = await _load_owned_conv(conversation_id, user_id, project_id)

    if allow_empty_source:
        if conversation_id is not None or temp_thread_id is not None:
            raise HTTPException(status_code=422, detail="empty source is only valid for a new temporary chat")
        source = {
            "messages": [],
            "message_ids": [],
            "source_hashes": [],
            "active_leaf_id": None,
            "revision": "temp:new",
            "checkpoint_id": None,
            "checkpoint": None,
        }
    else:
        try:
            source = await context_store.load_context_source(
                conversation_id=conversation_id,
                temp_thread_id=temp_thread_id,
                user_id=user_id,
                project_id=project_id,
                leaf_id=leaf_id,
            )
        except (cs.ConversationNotFound, LookupError) as exc:
            raise HTTPException(status_code=404, detail="temporary chat thread was not found") from exc
        except cs.ConversationForbidden as exc:
            raise HTTPException(status_code=403, detail="temporary chat thread is not accessible") from exc
        except cs.ChatStorageUnavailable as exc:
            raise HTTPException(status_code=503, detail="chat storage is unavailable") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail="chat storage is unavailable") from exc
        except ValueError as exc:
            if temp_thread_id is not None:
                raise HTTPException(status_code=503, detail="temporary chat history is unavailable") from exc
            raise HTTPException(status_code=422, detail="context source is invalid") from exc

    agent = await _resolve_agent(agent_id, user_id, project_id)
    frozen_budget = (
        None
        if is_preview or is_context_operation
        else await _validate_agent_budget(agent_budget, execution_mode=execution_mode, project_id=project_id, agent=agent)
    )
    resolved = await _resolve_model(model_id)
    extension_selection = await _resolve_extension_selection(agent, features, user_id=user_id, project_id=project_id)
    selected_plugin_tool_ids = (
        []
        if features.tool_policy.mode == "none"
        else _resolve_plugin_ids(agent, plugin_tool_ids, kind="tool", union=False)
    )
    selected_plugin_skill_ids = _resolve_plugin_ids(agent, plugin_skill_ids, kind="skill", union=True)
    plugin_tool_snapshots = await _freeze_plugin_bindings(
        selected_plugin_tool_ids, kind="tool", user_id=user_id, project_id=project_id
    )
    plugin_skill_snapshots = await _freeze_plugin_bindings(
        selected_plugin_skill_ids, kind="skill", user_id=user_id, project_id=project_id
    )
    if token_info is not None:
        if is_preview or is_context_operation:
            _require_context_read_scopes(
                token_info,
                features,
                skill_ids=skill_ids,
                agent=agent,
                plugin_skill_snapshots=plugin_skill_snapshots,
            )
        else:
            _require_native_admission_scopes(
                token_info,
                features,
                parts=parts,
                execution_mode=execution_mode,
                skill_ids=skill_ids,
                agent=agent,
                extension_selection=extension_selection,
                plugin_tool_snapshots=plugin_tool_snapshots,
                plugin_skill_snapshots=plugin_skill_snapshots,
            )

    feature_routes = await _resolve_feature_routes(features)
    _require_execution_capability(features, resolved, parts=parts, feature_routes=feature_routes)
    validated_reasoning_effort = _validated_reasoning_effort(reasoning_effort, resolved)
    _validate_tool_reasoning_compatibility(validated_reasoning_effort, resolved, features)

    skill_instructions, skill_snapshot = await _load_skill_snapshot(
        agent, skill_ids, plugin_skill_snapshots, user_id, project_id
    )
    draft_text = text_projection_from_user_input_parts(parts) if parts and append_draft else ""
    base_messages = _model_input(source.get("messages", []), extra_user=draft_text if draft_text else None)

    include_memory = bool(features.memory and conversation_id is not None)
    include_account = bool(token_info and token_info.get("auth_type") != "api_key") if token_info else True
    try:
        workspace_instr, memories = (
            await _load_context(
                conv,
                user_id,
                project_id,
                include_memory=include_memory,
                include_account_memory=include_account,
                memory_strategy=features.memory_retrieval,
                memory_query=draft_text,
            )
            if conv is not None
            else (None, [])
        )
    except ms.ChatStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail="chat storage is unavailable") from exc
    input_messages, temperature, max_tokens = _apply_context(
        agent,
        workspace_instr,
        memories,
        base_messages,
        None,
        None,
        skill_instructions=skill_instructions,
    )
    effective_max_tokens = min(max_tokens or _MAX_TOKENS_CAP, _MAX_TOKENS_CAP)

    summary_route = await resolve_summary_route(resolved)
    capability_snapshot, pricing_snapshot = _run_snapshots(
        resolved,
        features.model_dump(mode="json", by_alias=True),
        feature_routes=feature_routes,
        summary_route=summary_route,
    )
    capability_snapshot["extensions"] = _capability_extension_snapshot(extension_selection)
    if plugin_tool_snapshots or plugin_skill_snapshots:
        capability_snapshot["required_plugin_digest"] = get_registry().digest

    tool_schemas = await _preview_tool_schemas(
        features,
        extension_selection,
        user_id=user_id,
        project_id=project_id,
        plugin_tool_snapshots=plugin_tool_snapshots,
    )
    context_plan = _context_plan(
        features,
        extension_selection,
        memories=memories,
        workspace_instr=workspace_instr,
        workspace_id=(conv or {}).get("workspace_id"),
        skill_snapshot=skill_snapshot,
        agent=agent,
        parts=parts if append_draft else [],
        source=source,
        message_count=len(base_messages),
    )

    return {
        "input_messages": input_messages,
        "input_parts": [part.model_dump(mode="json", by_alias=True) for part in parts],
        "max_tokens": effective_max_tokens,
        "temperature": temperature,
        "reasoning_effort": validated_reasoning_effort,
        "source": source,
        "agent": agent,
        "resolved": resolved,
        "summary_route": summary_route,
        "capability_snapshot": capability_snapshot,
        "pricing_snapshot": pricing_snapshot,
        "feature_routes": feature_routes,
        "extension_selection": extension_selection,
        "skill_snapshot": skill_snapshot,
        "plugin_tool_snapshots": plugin_tool_snapshots,
        "tool_schemas": tool_schemas,
        "context_plan": context_plan,
        "workspace_instr": workspace_instr,
        "memories": memories,
        "agent_budget": frozen_budget,
    }
