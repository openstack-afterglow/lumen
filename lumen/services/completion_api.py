"""OpenAI/Anthropic 호환 외부 API 공통 완료 코어 (stateless).

웹 경로(`api/chat/completions.py` — 대화 트리 저장·자체 SSE 계약)와 **독립**이다. 서비스 계층만
재사용한다: `provider_store.resolve_model` → `credit.precheck` → `provider_client` 직접
스트리밍/비스트리밍(요청 `tools` pass-through) → `credit.apply_usage(source="api", api_key_id=…)`.
대화 트리에 저장하지 않는다(stateless). 도구는 서버가 실행하지 않고 `tool_calls` 를 릴레이한다
(표준 OpenAI/Anthropic function-calling — 호출자가 실행 후 재요청).

Provider invocation/normalization (`invoke_chat_once`, `invoke_responses_once`) is separate
from billing: online wrappers bill with a request-scoped UUID event, while durable Batch
runs (`durable_runs.api_completion`) settle the same normalized result against their
frozen run ledger with event ``run:<run_id>``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, Decimal
from typing import Any, Literal, NoReturn

import anyio

from lumen.config import get_settings
from lumen.models.billing import Payer
from lumen.services import (
    context_manager,
    credit,
    native_compaction,
    openai_tool_policy,
    provider_client,
    provider_metadata,
    stateless_credit,
)
from lumen.services.api_key_store import ApiKeyAuthorityUnavailable, ApiKeyForbidden
from lumen.services.capabilities import reasoning_can_be_disabled
from lumen.services.inference_authority import authorize_new_io, completion_scopes
from lumen.services.provider_errors import BadRequestError, OpenAIServiceTierMismatch, OpenAIUsageInvalid, ProviderError
from lumen.services.providers import errors
from lumen.services.providers import routing as ps
from lumen.services.providers.pricing import frozen_token_pricing
from lumen.services.providers.relay_history import receipt_owner
from lumen.services.usage_breakdown import (
    MODALITY_INPUT_REPORTING_PROVIDERS,
    UsageBreakdown,
    required_modalities_for_request,
)

logger = logging.getLogger(__name__)

_DEFAULT_MAX_TOKENS = 4096


def select_api_provider(body_provider: str | None, header_provider: str | None) -> str | None:
    if body_provider and header_provider and body_provider != header_provider:
        raise CompletionError(400, "provider_header_conflict")
    # A public model prefix describes its ID/transport encoding, not the
    # administrator's independently editable API selector.
    return body_provider or header_provider


class CompletionError(Exception):
    """포맷 무관 완료 오류 — 엔드포인트가 status_code 로 매핑."""

    def __init__(self, status_code: int, message: str, *, code: str | None = None):
        self.status_code = status_code
        self.message = message
        self.code = code
        super().__init__(message)


async def _authorize_completion(user_id: str, project_id: str, api_key_id: int | None, request: dict, resolved: dict) -> None:
    try:
        await authorize_new_io(user_id=user_id, project_id=project_id, api_key_id=api_key_id,
                               required_scopes=completion_scopes(request, resolved=resolved))
    except ApiKeyForbidden as exc:
        raise CompletionError(403, "inference_authority_revoked") from exc
    except ApiKeyAuthorityUnavailable as exc:
        raise CompletionError(503, "inference_authority_unavailable") from exc


async def resolve(model: str) -> dict:
    """모델 화이트리스트 해석(자격증명 포함). 미허용/미가용 시 CompletionError."""
    if not model:
        raise CompletionError(400, "model 이 필요합니다")
    try:
        resolved = await ps.resolve_model(model)
    except errors.AmbiguousModelRouteError as exc:
        raise CompletionError(409, "model_route_ambiguous") from exc
    except errors.ChatStorageUnavailable as exc:
        raise CompletionError(503, "일시적으로 사용할 수 없습니다") from exc
    if resolved is None:
        raise CompletionError(404, f"모델을 찾을 수 없습니다: {model}")
    return resolved


async def resolve_api(model: str, *, provider: str | None = None, operation: str = "chat") -> dict:
    """Resolve one public compatibility API route, rejecting ambiguous catalogs."""
    if not model:
        raise CompletionError(400, "model 이 필요합니다")
    try:
        resolved = await ps.resolve_api_model(model, provider=provider, required_capabilities=(operation,))
    except errors.AmbiguousModelRouteError as exc:
        raise CompletionError(409, "model_route_ambiguous") from exc
    except errors.ChatStorageUnavailable as exc:
        raise CompletionError(503, "일시적으로 사용할 수 없습니다") from exc
    if resolved is None:
        raise CompletionError(404, f"모델을 찾을 수 없습니다: {model}")
    return resolved


async def precheck(user_id: str, project_id: str, api_key_id: int | None = None, *, payer: Payer | None = None) -> None:
    """쿼터 fail-closed. 초과 429, 저장소 장애 503."""
    try:
        await credit.precheck(user_id, project_id, api_key_id=api_key_id, payer=payer)
    except credit.QuotaExceeded as exc:
        raise CompletionError(429, str(exc), code=getattr(exc, "code", None)) from exc
    except credit.ChatStorageUnavailable as exc:
        raise CompletionError(503, "일시적으로 사용할 수 없습니다") from exc


def clamp_max_tokens(requested: int | None) -> int:
    """Apply only the legacy default; explicit positive budgets are never capped."""
    value = _DEFAULT_MAX_TOKENS if requested is None else requested
    if value <= 0:
        raise CompletionError(422, "max token budget must be positive")
    return value


def _official_openai(resolved: dict) -> bool:
    # Routing, validation and tariff selection share the same exact endpoint check.
    return resolved.get("provider_type") == "openai" and provider_metadata.direct_provider_route(
        "openai", resolved.get("api_base")
    )


def chat_provider_options(options: dict[str, Any], *, resolved: dict) -> dict[str, Any]:
    validated = _openai_provider_options(options, resolved=resolved, effort=options.get("reasoning_effort"))
    return validated if _official_openai(resolved) else {}


def _openai_provider_options(options: dict[str, Any], *, resolved: dict, effort: Any) -> dict[str, Any]:
    options = dict(options)
    if options.pop("service_tier", None) not in (None, "default", "auto"):
        raise CompletionError(422, "Only Standard processing is supported.", code="service_tier_unsupported")
    if not _official_openai(resolved):
        options.pop("prompt_cache_options", None)
        options.pop("prompt_cache_retention", None)
        options.pop("max_tool_calls", None)
        return options
    if "comparison_response_id" in (options.get("prompt_cache_options") or {}):
        raise CompletionError(
            422, "References to provider-side objects are not supported.", code="provider_reference_unsupported",
        )
    options["service_tier"] = "default"
    catalog = provider_metadata.native_catalog_entry(resolved["model_name"], "openai", resolved.get("api_base"))
    caps = resolved.get("capabilities") or {}
    # The native model contract is authoritative where it publishes choices.
    reasoning_options = catalog.get("reasoning_options") or caps.get("reasoning_options") or []
    efforts = {value for option in reasoning_options if option["type"] == "effort" for value in option["values"]}
    if effort is not None and (
        not isinstance(effort, str)
        or (efforts and effort not in efforts)
        or (not efforts and effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max"})
    ):
        raise CompletionError(422, "Unsupported reasoning effort for this model.", code="reasoning_effort_unsupported")
    if "prompt_cache_options" in options and "prompt_cache_retention" in options:
        raise CompletionError(422, "Specify only one prompt-cache lifetime field.", code="prompt_cache_options_conflict")
    pricing = catalog.get("token_pricing") or {}
    if pricing.get("cache_write_ttl") == "30m" and "prompt_cache_retention" in options:
        del options["prompt_cache_retention"]
        options["prompt_cache_options"] = {"ttl": "30m"}
    elif options.get("prompt_cache_retention") == "in-memory":
        options["prompt_cache_retention"] = "in_memory"
    return options


def validate_openai_input(resolved: dict, messages: str | list[dict]) -> None:
    if not _official_openai(resolved):
        return
    try:
        openai_tool_policy.validate_input(messages)
    except openai_tool_policy.UnsupportedProviderReference:
        raise CompletionError(
            422, "References to provider-side objects are not supported.", code="provider_reference_unsupported",
        ) from None
    except openai_tool_policy.UnsupportedHostedTool:
        raise CompletionError(422, "Unsupported hosted tool in input.", code="hosted_tool_unsupported") from None


def _bounded_openai_model(route: dict) -> dict:
    return provider_metadata.bounded_openai_text_model(
        route["model_name"], route.get("provider_type"), route.get("api_base"),
    )


def _chat_wire_budget(resolved: dict, requested: int | None) -> int | None:
    if requested is None and (_official_openai(resolved) or resolved.get("provider_type") == "chatgpt"):
        return None
    return clamp_max_tokens(requested)


def _chat_hold_output_budget(resolved: dict, wire_budget: int | None) -> int:
    return wire_budget if wire_budget is not None else (
        _bounded_openai_model(resolved).get("max_output_tokens") or _DEFAULT_MAX_TOKENS
    )


# Published patch caps multiplied and rounded up; see OpenAI's vision guide.
_OPENAI_PATCH_IMAGE_CAPS = {
    **dict.fromkeys(("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"), (308, 3_000, 36_000, 36_000)),
    "gpt-5.5": (308, 3_000, 12_000, 12_000),
    **dict.fromkeys(("gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano"), (7_373, 3_000, 12_000, 3_000)),
    "gpt-5.2": (7_373, 7_373, None, 7_373),
}
_OPENAI_TILED_IMAGE_RATES = {
    "gpt-5.1": (70, 140), "gpt-5": (70, 140),
    "gpt-4o": (85, 170), "gpt-4.1": (85, 170), "gpt-4o-mini": (2833, 5667), "o3": (75, 150),
}


def _openai_image_bound(model: str, detail: object) -> int | None:
    levels = ("low", "high", "original", "auto")
    if detail is None:
        detail = "auto"
    if detail not in levels:
        return None
    if patch_caps := _OPENAI_PATCH_IMAGE_CAPS.get(model):
        return patch_caps[levels.index(detail)]
    if detail != "original" and (tile_rates := _OPENAI_TILED_IMAGE_RATES.get(model)):
        base, tile = tile_rates
        return base if detail == "low" else base + 16 * tile  # at most 4×4 tiles after 2048px fit
    return None


def _request_input_bound(
    route: dict, model: dict, *, messages: list[dict], tools: list[dict] | None, max_output: int,
    prompt_options: dict, content_growth: bool,
) -> tuple[int, bool]:
    if max_output > model["max_output_tokens"]:
        raise CompletionError(422, "Output token budget exceeds the model limit.", code="max_output_tokens_exceeded")
    ceiling = model.get("max_input_tokens") or model["context_window"]
    if ceiling <= 0:
        raise CompletionError(422, "Input exceeds the model input limit.", code="context_length_exceeded")
    prompt = {
        "messages": messages,
        "tools": tools or [],
        **{name: prompt_options[name] for name in ("text", "response_format", "tool_choice") if name in prompt_options},
    }
    may_write_long = (prompt_options.get("prompt_cache_options") or {}).get("ttl") == "12h"
    pending: list[Any] = [prompt]
    image_tokens = image_wire_bytes = 0
    while pending:
        item = pending.pop()
        if isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, dict):
            kind = item.get("type")
            if kind in ("input_image", "image_url", "computer_screenshot") and not content_growth:
                url = item.get("image_url")
                detail = url.get("detail") if isinstance(url, dict) else item.get("detail")
                image_bound = _openai_image_bound(model["id"], detail)
                if image_bound is None:
                    content_growth = True
                else:
                    image_tokens += image_bound
                    url = url.get("url") if isinstance(url, dict) else url
                    if isinstance(url, str):
                        # The JSON envelope includes the pixels/URL, which are not prompt text.
                        image_wire_bytes += len(json.dumps(url, ensure_ascii=False).encode("utf-8", errors="surrogatepass")) - 2
            elif kind in ("image", "file", "input_file", "input_audio", "audio_url"):
                content_growth = True
            breakpoint = item.get("prompt_cache_breakpoint")
            if isinstance(breakpoint, dict) and breakpoint.get("ttl") == "12h":
                may_write_long = True
            if kind in ("mcp", "tool_search"):
                content_growth = True
            pending.extend(value for key, value in item.items() if key not in ("input_schema", "parameters", "schema"))
    # Framing is a Lumen allowance, not a vendor tokenizer guarantee. Count
    # encrypted reasoning bytes assuming ciphertext bytes >= replayed tokens.
    # Usage over the bound cannot settle.
    bound = ceiling
    if not content_growth:
        try:
            size = len(json.dumps(prompt, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
        except (ValueError, UnicodeError):
            raise CompletionError(422, "Invalid OpenAI request content.", code="invalid_request_body") from None
        bound = min(ceiling, size - image_wire_bytes + image_tokens + 4_096 + 16 * (len(messages) + len(tools or ())))
    # A full funding envelope does not exempt countable input from admission.
    if bound == ceiling and model.get("max_input_tokens") is not None:
        count = provider_metadata.count_context_tokens(route["model_name"], messages, tools).tokens
        if count is not None and count > ceiling:
            raise CompletionError(422, "Input exceeds the model input limit.", code="context_length_exceeded")
    return bound, may_write_long


@dataclass
class _OnlineReservation:
    event_id: str
    credit_per_usd: Decimal
    input_bound: int
    output_bound: int
    payer: Payer | None = None
    dispatched: bool = False
    settled: bool = False
    settling: bool = False

    async def aclose(self) -> None:
        if not self.settled:
            with anyio.CancelScope(shield=True):
                await stateless_credit.finish(self.event_id, uncertain=self.dispatched, payer=self.payer)
            self.settled = True


def _provider_rejected(hold: _OnlineReservation | None, exc: Exception) -> None:
    if hold is not None and (
        isinstance(exc, ProviderError) and exc.known_rejection or isinstance(exc, errors.ProviderSubscriptionError)
    ):
        hold.dispatched = False


async def _reserve_online(
    route: dict, *, messages: list[dict], tools: list[dict] | None, max_output: int,
    user_id: str, project_id: str, api_key_id: int | None,
    payer: Payer | None = None,
    tool_bound_usd: Decimal = Decimal("0"),
    prompt_options: dict | None = None,
) -> _OnlineReservation | None:
    model = _bounded_openai_model(route)
    if model:
        input_bound, may_write_long = _request_input_bound(
            route, model, messages=messages, tools=tools, max_output=max_output,
            prompt_options=prompt_options or {}, content_growth=bool(tool_bound_usd),
        )
    elif payer is not None and not tool_bound_usd:
        # Outside the bounded OpenAI contract, reserve the entire configured
        # model context. A local tokenizer estimate is not a funding envelope.
        capabilities = route.get("capabilities") or {}
        input_bound = capabilities.get("context_limit")
        if (type(input_bound) is not int or input_bound <= 0 or max_output <= 0
                or capabilities.get("output_budget_enforced") is False):
            raise CompletionError(422, "Model context or output bound unavailable.", code="pricing_unavailable")
        max_allowed_output = capabilities.get("max_output_tokens")
        if type(max_allowed_output) is int and max_output > max_allowed_output:
            raise CompletionError(422, "Output token budget exceeds the model limit.", code="max_output_tokens_exceeded")
        may_write_long = True
    elif tool_bound_usd:
        raise CompletionError(422, "Hosted tool pricing requires a bounded model.", code="pricing_unavailable")
    else:
        return None
    pricing = frozen_token_pricing(route)
    if pricing["input_price_per_token"] is None or pricing["output_price_per_token"] is None:
        raise CompletionError(422, "Model pricing unavailable.", code="pricing_unavailable")
    if input_bound <= pricing["long_context_threshold_tokens"]:
        pricing = {key: value for key, value in pricing.items() if not key.endswith("_above_200k")}
    if model and (model.get("token_pricing") or {}).get("cache_write_long_ttl") == "12h" and not may_write_long:
        pricing.pop("cache_write_1h_price_per_token", None)
        pricing.pop("cache_write_1h_price_per_token_above_200k", None)
    conversion = Decimal(str(get_settings().chat_credit_per_usd))
    bound = credit.call_credit_bound(
        {**pricing, "chat_credit_per_usd": conversion, "margin_multiplier": route["margin_multiplier"]},
        input_tokens=input_bound, output_tokens=max_output,
    )
    if tool_bound_usd:
        # Round the fee envelope outward without relying on ledger rounding.
        bound += (tool_bound_usd * Decimal(str(route["margin_multiplier"])) * conversion).quantize(
            Decimal("0.00000001"), rounding=ROUND_CEILING,
        )
    hold = _OnlineReservation(str(uuid.uuid4()), conversion, input_bound, max_output, payer=payer)
    try:
        await stateless_credit.reserve(
            event_id=hold.event_id, user_id=user_id, project_id=project_id, api_key_id=api_key_id, bound=bound,
            payer=payer,
        )
    except credit.QuotaExceeded as exc:
        raise CompletionError(429, str(exc), code=getattr(exc, "code", None)) from exc
    except credit.ChatStorageUnavailable as exc:
        raise CompletionError(503, "Credit reservation unavailable.") from exc
    return hold


def _unknown_provider_result(exc: ProviderError) -> CompletionError:
    return CompletionError(502, exc.code, code="provider_result_unknown")



def billing_route(
    resolved: dict, messages: list[dict], *, protocol: str, options: dict | None = None
) -> dict:
    """Freeze this stateless request's route/prices and reject unmeterable priced media.

    The chat allowlist is evidence about its transport, not a model-name guess.
    Native protocol adapters do not establish equivalent modality metering.
    Durable Batch and online official OpenAI routes with known limits add a hold.
    """

    route = deepcopy(resolved)
    try:
        token_pricing = frozen_token_pricing(route)
        route["token_rates"] = token_pricing["token_rates"]
        route["token_rates_above_200k"] = token_pricing.get("token_rates_above_200k") or {}
        route["token_rates_follow_text"] = token_pricing.get("token_rates_follow_text") is True
    except errors.ProviderValidationError as exc:
        raise CompletionError(422, "model token pricing is invalid") from exc
    output_modalities = list((options or {}).get("modalities") or [])
    if protocol == "chat":
        validate_openai_input(route, messages)
    if protocol == "anthropic" and _official_openai(route):
        if (options or {}).get("container") is not None or any(
            tool.get("type") not in (None, "custom") for tool in (options or {}).get("tools") or ()
        ):
            raise CompletionError(422, "hosted_tool_unsupported", code="hosted_tool_unsupported")
        validate_openai_input(route, messages)
        if _bounded_openai_model(route):
            # Messages SSE does not preserve the original Responses accounting evidence.
            raise CompletionError(422, "messages_accounting_unsupported", code="messages_accounting_unsupported")
    if protocol == "chat" and _official_openai(route) and any(
        tool.get("type") not in ("function", "custom") for tool in (options or {}).get("tools") or ()
    ):
        raise CompletionError(
            422, "Use Responses for hosted tools.", code="hosted_tool_unsupported",
        )
    if any(tool.get("type") == "image_generation" for tool in (options or {}).get("tools") or []):
        output_modalities.append("image")
    required = required_modalities_for_request(
        route["token_rates"], messages=messages, output_modalities=output_modalities
    )
    route["required_token_modalities"] = required
    reporting = (
        MODALITY_INPUT_REPORTING_PROVIDERS.get(str(route.get("provider_type") or ""), frozenset())
        if protocol in {"chat", "gemini"} and (route.get("provider_auth") or {}).get("auth_mode", "api_key") == "api_key"
        else frozenset()
    )
    for item in required:
        modality, direction = item.rsplit("_", 1)
        if direction != "input" or modality not in reporting:
            raise CompletionError(422, f"{item} (modality_usage_unavailable)")
    return route


def usage_breakdown_for_billing(
    model_name: str,
    *,
    token_rates: dict | None,
    required: list[str] | tuple[str, ...],
    messages: list[dict],
    text: str,
    final_usage: Any | None,
) -> UsageBreakdown:
    """Validate provider usage against the frozen modality requirements of one call.

    A priced media split must be explicitly reported; malformed usage fails
    instead of being guessed. Only when nothing requires an exact split may an
    absent report fall back to the local token estimate.
    """
    breakdown = UsageBreakdown.from_runtime(final_usage)
    if (required or (token_rates and final_usage is not None)) and (
        breakdown is None or breakdown.modality_usage_invalid
    ):
        raise CompletionError(502, "modality_usage_unavailable")
    if required:
        def field(value: Any, key: str) -> Any:
            return value.get(key) if isinstance(value, dict) else getattr(value, key, None)

        # A zero aggregate is not evidence that the provider explicitly
        # reported a requested media split. Do not synthesize even zero media.
        canonical = field(final_usage, "modality_tokens")
        for item in required:
            modality, direction = item.rsplit("_", 1)
            if canonical is not None:
                reported = field(field(canonical, modality), f"{direction}_tokens")
            else:
                details = "prompt_tokens_details" if direction == "input" else "completion_tokens_details"
                reported = field(field(final_usage, details), f"{modality}_tokens")
            if reported is None:
                raise CompletionError(502, "modality_usage_unavailable")
    if breakdown is None:
        breakdown = provider_metadata.extract_usage_breakdown(model_name, messages, text, final_usage)
    return breakdown


async def _bill(
    resolved: dict,
    messages: list[dict],
    text: str,
    final_usage: Any | None,
    *,
    event_id: str,
    user_id: str,
    project_id: str,
    api_key_id: int | None,
    hold: _OnlineReservation | None = None,
    tool_charges: dict | None = None,
) -> tuple[int, int, Decimal]:
    """extract_usage → cost_from_usage → apply_usage(source="api"). (pt, ct, credited) 반환.

    event_id 는 요청당 1개를 재사용한다(정상/finally 재과금 모두 동일 id) — apply_usage 의
    event_id unique + IntegrityError 흡수로 정확히 1회 과금(중복 방지). 새 uuid 마다 생성하면
    이 멱등성이 깨져 이중 과금·통계 이중집계가 발생한다.
    """
    model_name = resolved["model_name"]
    token_rates = resolved.get("token_rates")
    token_rates_above_200k = resolved.get("token_rates_above_200k")
    token_rates_follow_text = resolved.get("token_rates_follow_text") is True
    if token_rates is None:
        token_pricing = frozen_token_pricing(resolved)
        token_rates = token_pricing["token_rates"]
        token_rates_above_200k = token_pricing.get("token_rates_above_200k")
        token_rates_follow_text = token_pricing.get("token_rates_follow_text") is True
    required = resolved.get("required_token_modalities")
    if required is None:
        required = required_modalities_for_request(token_rates, messages=messages)
    if hold is not None:
        reported = UsageBreakdown.from_runtime(final_usage)
        if reported is None or reported.modality_usage_invalid:
            raise _unknown_provider_result(OpenAIUsageInvalid())
    breakdown = usage_breakdown_for_billing(
        model_name, token_rates=token_rates, required=required, messages=messages, text=text,
        final_usage=final_usage,
    )
    pt, ct = breakdown.input_tokens, breakdown.output_tokens
    if hold is not None:
        logger.info(
            "Held usage reservation_id=%s input_tokens=%d input_bound=%d output_tokens=%d output_bound=%d",
            hold.event_id, pt, hold.input_bound, ct, hold.output_bound,
        )
        if pt > hold.input_bound or ct > hold.output_bound:
            raise CompletionError(502, "usage_exceeds_reservation", code="provider_result_unknown")
    try:
        usage_cost = provider_metadata.cost_from_usage(
            model_name,
            pt,
            ct,
            input_price_per_token=resolved.get("input_price_per_token"),
            output_price_per_token=resolved.get("output_price_per_token"),
            input_price_per_token_above_200k=resolved.get("input_price_per_token_above_200k"),
            output_price_per_token_above_200k=resolved.get("output_price_per_token_above_200k"),
            long_context_threshold_tokens=resolved.get("long_context_threshold_tokens", 200_000),
            price_source=resolved.get("price_source"),
            provider_type=resolved.get("provider_type"),
            api_base=resolved.get("api_base"),
            breakdown=breakdown,
            cache_read_price_per_token=resolved.get("cache_read_price_per_token"),
            cache_write_price_per_token=resolved.get("cache_write_price_per_token"),
            cache_write_1h_price_per_token=resolved.get("cache_write_1h_price_per_token"),
            cache_read_price_per_token_above_200k=resolved.get("cache_read_price_per_token_above_200k"),
            cache_write_price_per_token_above_200k=resolved.get("cache_write_price_per_token_above_200k"),
            cache_write_1h_price_per_token_above_200k=resolved.get("cache_write_1h_price_per_token_above_200k"),
            cache_price_sources=resolved.get("cache_price_sources"),
            allow_catalog_cache=False,
            allow_catalog_prices=False,
            token_rates=token_rates,
            token_rates_above_200k=token_rates_above_200k,
            token_rates_follow_text=token_rates_follow_text,
            required_modalities=required,
        )
    except ValueError as exc:
        if hold is not None:
            raise _unknown_provider_result(OpenAIUsageInvalid()) from exc
        if token_rates or required:
            raise CompletionError(502, "modality_usage_unavailable") from exc
        raise
    if hold is not None and usage_cost.pricing_status != "priced":
        raise _unknown_provider_result(OpenAIUsageInvalid())
    if tool_charges:
        fee = sum(Decimal(charge["cost"]) for charge in tool_charges.values())
        usage_cost = replace(
            usage_cost, raw_cost=usage_cost.raw_cost + fee,
            pricing_snapshot={**usage_cost.pricing_snapshot, "hosted_tools": tool_charges},
        )
    settle = stateless_credit.settle if hold is not None else credit.apply_usage
    if hold is not None:
        hold.settling = True
    try:
        # Once we have terminal accounting evidence, finish the atomic write.
        with anyio.CancelScope(shield=hold is not None):
            credited = await settle(
                event_id=event_id,
                user_id=user_id,
                project_id=project_id,
                model_name=model_name,
                provider=resolved.get("provider_name"),
                prompt_tokens=pt,
                completion_tokens=ct,
                usage_cost=usage_cost,
                margin_multiplier=resolved["margin_multiplier"],
                conversation_id=None,
                source="api",
                api_key_id=api_key_id,
                execution_route=resolved,
                breakdown=breakdown,
                **({"credit_per_usd": hold.credit_per_usd, "payer": hold.payer} if hold is not None else {}),
            )
    except stateless_credit.ChargeExceedsReservation as exc:
        raise CompletionError(502, "charge_exceeds_reservation", code="provider_result_unknown") from exc
    finally:
        if hold is not None:
            hold.settling = False
    if hold is not None:
        hold.settled = True
    return pt, ct, credited


def _norm_tool_calls(raw: Any) -> list[dict] | None:
    """litellm tool_calls(객체/부분델타) → 안정 dict 리스트. 없으면 None."""
    if not raw:
        return None
    out = []
    for i, tc in enumerate(raw):
        fn = getattr(tc, "function", None) or (tc.get("function") if isinstance(tc, dict) else None) or {}
        out.append(
            {
                "index": getattr(tc, "index", None) if not isinstance(tc, dict) else tc.get("index", i),
                "id": getattr(tc, "id", None) if not isinstance(tc, dict) else tc.get("id"),
                "type": "function",
                "function": {
                    "name": getattr(fn, "name", None) if not isinstance(fn, dict) else fn.get("name"),
                    "arguments": getattr(fn, "arguments", None) if not isinstance(fn, dict) else fn.get("arguments"),
                },
            }
        )
    return out


def _completion_provider_options(
    options: dict | None, *, resolved: dict, client_functions_only: bool,
) -> dict:
    if client_functions_only and not _official_openai(resolved):
        # The Gemini adapter closed-validates and translates the option vocabulary.
        return options or {}
    return chat_provider_options(options or {}, resolved=resolved)


def _reasoning_effort(explicit: str | None, resolved: dict | None = None) -> str | None:
    """Explicit effort, else the operator default.

    An operator ``none`` carries no per-request intent, so it takes the omission path
    (provider default) for models that cannot disable reasoning instead of sending a
    value such as gpt-5/o3 reject.
    """
    if explicit:
        return explicit
    effort = get_settings().chat_reasoning_effort
    if (
        resolved is not None
        and isinstance(effort, str)
        and effort.strip().lower() == "none"
        and not reasoning_can_be_disabled(resolved.get("capabilities"), resolved.get("provider_type"))
    ):
        return None
    return effort


@dataclass(frozen=True)
class ChatInvocation:
    """Normalized nonstreaming provider chat result; carries raw usage, never a charge."""

    model: str
    content: str
    tool_calls: list[dict] | None
    finish_reason: str
    usage: Any


async def invoke_chat_once(
    *,
    resolved: dict,
    messages: list[dict],
    max_tokens: int | None,
    temperature: float | None,
    tools: list[dict] | None = None,
    tool_choice: Any = None,
    options: dict | None = None,
    provider_once: bool = False,
) -> ChatInvocation:
    """Send one nonstreaming chat request on an already frozen route; no billing.

    Provider exceptions propagate unchanged so each caller keeps its own error
    contract. ``provider_once`` disables every transport retry/re-send layer.
    """
    extra_kwargs: dict[str, Any] = {**(options or {}), "capabilities_override": resolved.get("capabilities") or {}}
    if tool_choice is not None:
        extra_kwargs["tool_choice"] = tool_choice
    resp = await provider_client.acompletion(
        resolved["model_name"],
        messages,
        api_base=resolved.get("api_base"),
        api_key=resolved.get("api_key"),
        custom_llm_provider=resolved.get("provider_type"),
        max_tokens=max_tokens,
        temperature=temperature,
        tools=tools,
        extra=extra_kwargs or None,
        provider_auth=resolved.get("provider_auth"),
        **({"provider_once": True} if provider_once else {}),
    )
    if _official_openai(resolved) and isinstance(resp, dict):
        provider_client.check_openai_tier(resp)
    choice = resp.choices[0]
    msg = choice.message
    return ChatInvocation(
        model=resolved["api_model_name"],
        content=getattr(msg, "content", None) or "",
        tool_calls=_norm_tool_calls(getattr(msg, "tool_calls", None)),
        finish_reason=getattr(choice, "finish_reason", None) or "stop",
        usage=getattr(resp, "usage", None),
    )


@receipt_owner
async def complete_once(
    *,
    resolved: dict,
    messages: list[dict],
    user_id: str,
    project_id: str,
    api_key_id: int | None,
    payer: Payer | None = None,
    max_tokens: int | None,
    temperature: float | None,
    tools: list[dict] | None = None,
    tool_choice: Any = None,
    options: dict | None = None,
    client_functions_only: bool = False,
) -> dict:
    """비스트리밍 완료 — 전체 응답 반환 + 과금. tool_calls 는 릴레이(서버 미실행)."""
    resolved = billing_route(resolved, messages, protocol="chat", options={"tools": tools})
    await _authorize_completion(user_id, project_id, api_key_id, {} if client_functions_only else {"tools": tools}, resolved)
    options = _completion_provider_options(options, resolved=resolved, client_functions_only=client_functions_only)
    max_output = _chat_wire_budget(resolved, max_tokens)
    if payer is not None and max_output is None:
        max_output = _chat_hold_output_budget(resolved, None)
    hold = await _reserve_online(
        resolved, messages=messages, tools=tools, max_output=_chat_hold_output_budget(resolved, max_output),
        user_id=user_id, project_id=project_id, api_key_id=api_key_id,
        payer=payer,
        prompt_options={**options, "tool_choice": tool_choice},
    )
    try:
        if hold is not None:
            hold.dispatched = True
        invocation = await invoke_chat_once(
            resolved=resolved,
            messages=messages,
            max_tokens=max_output,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
            options=options,
        )
        pt, ct, credited = await _bill(
            resolved, messages, invocation.content, invocation.usage,
            event_id=hold.event_id if hold else str(uuid.uuid4()), user_id=user_id, project_id=project_id,
            api_key_id=api_key_id, hold=hold,
        )
    except errors.ProviderSubscriptionError as exc:
        _provider_rejected(hold, exc)
        raise CompletionError(exc.status_code, exc.message) from None
    except (OpenAIServiceTierMismatch, OpenAIUsageInvalid) as exc:
        raise _unknown_provider_result(exc) from None
    except ProviderError as exc:
        _provider_rejected(hold, exc)
        raise
    finally:
        if hold is not None:
            await hold.aclose()
    return {
        "model": invocation.model,
        "content": invocation.content,
        "tool_calls": invocation.tool_calls,
        "finish_reason": invocation.finish_reason,
        "prompt_tokens": pt,
        "completion_tokens": ct,
        **(UsageBreakdown.from_runtime(invocation.usage) or UsageBreakdown(0, 0)).cache_fields(),
        "openai_cache_ttl": _official_openai(resolved),
        "credited_cost": float(credited),
    }


async def complete_stream(
    *,
    resolved: dict,
    messages: list[dict],
    user_id: str,
    project_id: str,
    api_key_id: int | None,
    payer: Payer | None = None,
    max_tokens: int | None,
    temperature: float | None,
    tools: list[dict] | None = None,
    tool_choice: Any = None,
    options: dict | None = None,
    client_functions_only: bool = False,
) -> AsyncIterator[dict]:
    """Validate/freeze before HTTP streaming starts, then return the completion iterator."""
    resolved = billing_route(resolved, messages, protocol="chat", options={"tools": tools})
    options = _completion_provider_options(options, resolved=resolved, client_functions_only=client_functions_only)
    max_output = _chat_wire_budget(resolved, max_tokens)
    if payer is not None and max_output is None:
        max_output = _chat_hold_output_budget(resolved, None)
    await _authorize_completion(user_id, project_id, api_key_id, {} if client_functions_only else {"tools": tools}, resolved)
    hold = await _reserve_online(
        resolved, messages=messages, tools=tools, max_output=_chat_hold_output_budget(resolved, max_output),
        user_id=user_id, project_id=project_id, api_key_id=api_key_id,
        payer=payer,
        prompt_options={**options, "tool_choice": tool_choice},
    )
    events = _complete_stream(
        resolved=resolved, messages=messages, user_id=user_id, project_id=project_id,
        api_key_id=api_key_id, max_tokens=max_output, temperature=temperature,
        tools=tools, tool_choice=tool_choice, options=options, hold=hold,
    )
    return _ProviderEvents(events, None, hold) if hold is not None else events


@receipt_owner
async def _complete_stream(
    *,
    resolved: dict,
    messages: list[dict],
    user_id: str,
    project_id: str,
    api_key_id: int | None,
    max_tokens: int | None,
    temperature: float | None,
    tools: list[dict] | None = None,
    tool_choice: Any = None,
    options: dict,
    hold: _OnlineReservation | None,
) -> AsyncIterator[dict]:
    """스트리밍 완료 — 정규화 델타 yield 후 최종 done. 종료 시 과금(정확히 1회).

    yield 형태:
      {"type":"delta", "content":str, "reasoning":str, "tool_calls":list|None, "finish_reason":str|None}
      {"type":"done", "prompt_tokens":int, "completion_tokens":int, "finish_reason":str, "credited_cost":float}
      {"type":"error", "message":str}
    """
    text_parts: list[str] = []
    final_usage = None
    finish_reason = "stop"
    charged = False
    gen = None
    event_id = hold.event_id if hold is not None else str(uuid.uuid4())
    extra_kwargs: dict[str, Any] = {**options, "capabilities_override": resolved.get("capabilities") or {}}
    if tool_choice is not None:
        extra_kwargs["tool_choice"] = tool_choice
    # One finally owns the provider stream and partial billing: a consumer close
    # (GeneratorExit) at any delta yield must still close upstream and bill.
    try:
        try:
            if hold is not None:
                hold.dispatched = True
            gen = await provider_client.acompletion_stream(
                resolved["model_name"],
                messages,
                api_base=resolved.get("api_base"),
                api_key=resolved.get("api_key"),
                custom_llm_provider=resolved.get("provider_type"),
                max_tokens=max_tokens,
                temperature=temperature,
                tools=tools,
                extra=extra_kwargs or None,
                reasoning_effort=_reasoning_effort(options.get("reasoning_effort"), resolved),
                provider_auth=resolved.get("provider_auth"),
            )
            async for chunk in gen:
                if _official_openai(resolved) and isinstance(chunk, dict):
                    provider_client.check_openai_tier(chunk)
                u = getattr(chunk, "usage", None)
                if u is not None:
                    final_usage = u
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                ch = choices[0]
                delta = getattr(ch, "delta", None)
                fr = getattr(ch, "finish_reason", None)
                if fr:
                    finish_reason = fr
                content = (getattr(delta, "content", None) if delta else None) or ""
                reasoning = (getattr(delta, "reasoning_content", None) if delta else None) or ""
                tcs = _norm_tool_calls(getattr(delta, "tool_calls", None) if delta else None)
                if content:
                    text_parts.append(content)
                if content or reasoning or tcs or fr:
                    yield {
                        "type": "delta",
                        "content": content,
                        "reasoning": reasoning,
                        "tool_calls": tcs,
                        "finish_reason": fr,
                    }
        except errors.ProviderSubscriptionError as exc:
            _provider_rejected(hold, exc)
            logger.warning("API 구독 스트리밍 실패 model=%s code=%s", resolved.get("model_name"), exc.code)
            yield {"type": "error", "code": exc.code, "message": exc.message}
            return
        except ProviderError as exc:
            _provider_rejected(hold, exc)
            if isinstance(exc, (OpenAIServiceTierMismatch, OpenAIUsageInvalid)):
                # The HTTP consumer closes immediately after receiving the error.
                text_parts.clear()
            if hold is not None and hold.dispatched:
                yield {"type": "error", "code": "provider_result_unknown", "message": exc.code}
            else:
                yield {"type": "error", "message": "생성 중 오류가 발생했습니다"}
            return
        except Exception:
            logger.warning("API 스트리밍 실패 model=%s", resolved.get("model_name"), exc_info=True)
            yield {"type": "error", "message": "생성 중 오류가 발생했습니다"}
            return

        try:
            pt, ct, credited = await _bill(
                resolved, messages, "".join(text_parts), final_usage, event_id=event_id,
                user_id=user_id, project_id=project_id, api_key_id=api_key_id, hold=hold,
            )
        except CompletionError as exc:
            if hold is None:
                raise
            yield {"type": "error", "code": exc.code, "message": exc.message}
            return
        charged = True
        yield {
            "type": "done",
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "finish_reason": finish_reason,
            "credited_cost": float(credited),
            **(UsageBreakdown.from_runtime(final_usage) or UsageBreakdown(0, 0)).cache_fields(),
            "openai_cache_ttl": _official_openai(resolved),
        }
    finally:
        # An ASGI disconnect is a level-triggered AnyIO cancel that can land at the
        # upstream read; unshielded cleanup awaits would be re-cancelled and skip billing.
        with anyio.CancelScope(shield=True):
            try:
                await _close_provider_stream(gen)
            finally:
                if hold is not None:
                    await hold.aclose()
                elif not charged and text_parts and not resolved.get("token_rates"):
                    try:
                        await _bill(
                            resolved,
                            messages,
                            "".join(text_parts),
                            final_usage,
                            event_id=event_id,
                            user_id=user_id,
                            project_id=project_id,
                            api_key_id=api_key_id,
                        )
                    except Exception:
                        logger.warning("API 스트림 종료 후 과금 실패", exc_info=True)


async def _close_provider_stream(stream: Any) -> None:
    """Release an opened provider stream; LiteLLM Responses iterators expose only their httpx response."""
    if stream is None:
        return
    close = getattr(stream, "aclose", None)
    if not callable(close):
        close = getattr(getattr(stream, "response", None), "aclose", None)
    if not callable(close):
        return
    try:
        await close()
    except Exception:
        logger.warning("provider stream close failed", exc_info=True)


_HELD_STREAM_DRAIN_SECONDS = 15.0


class _ProviderEvents:
    """Native events that release their eagerly opened provider stream even if never iterated.

    Routes register this with the ASGI SSE owner, whose cleanup runs even when the
    response body generator never starts. ``aclose`` is idempotent.
    """

    def __init__(self, events: AsyncIterator[dict], provider_stream: Any, hold: _OnlineReservation | None = None):
        self._events = events
        self._provider_stream = provider_stream
        self._hold = hold
        self._pending: asyncio.Task | None = None
        self._ended = False
        self._closed = False

    @property
    def owns_disconnect_drain(self) -> bool:
        return self._hold is not None

    def __aiter__(self) -> _ProviderEvents:
        return self

    async def __anext__(self) -> dict:
        if self._closed or self._ended:
            raise StopAsyncIteration
        if self._hold is None:
            return await anext(self._events)
        return await self._next_held()

    async def _next_held(self) -> dict:
        # Client cancellation must not be injected into an active provider read.
        if self._pending is None:
            self._pending = asyncio.create_task(anext(self._events))
        try:
            item = await asyncio.shield(self._pending)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._pending = None
            self._ended = True
            raise
        self._pending = None
        return item

    async def _drain_held(self) -> None:
        if self._hold is None or not self._hold.dispatched or self._hold.settled or self._ended:
            return
        with anyio.move_on_after(_HELD_STREAM_DRAIN_SECONDS), suppress(StopAsyncIteration):
            while not self._hold.settled:
                await self._next_held()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        with anyio.CancelScope(shield=True):
            try:
                await self._drain_held()
            except Exception:
                logger.warning("provider stream failed after client disconnect")
            finally:
                if self._pending is not None:
                    # The drain deadline stops provider reads, not terminal settlement.
                    if self._hold is None or not self._hold.settling:
                        self._pending.cancel()
                    await asyncio.gather(self._pending, return_exceptions=True)
                    self._pending = None
                try:
                    await self._events.aclose()
                finally:
                    try:
                        await _close_provider_stream(self._provider_stream)
                    finally:
                        if self._hold is not None:
                            await self._hold.aclose()


def _native_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json", exclude_none=True)
    raise CompletionError(502, "upstream returned an invalid native response")


def _native_text(value: Any) -> str:
    if isinstance(value, dict):
        kind = value.get("type")
        if kind in {"output_text", "text"} and isinstance(value.get("text"), str):
            return value["text"]
        return "".join(_native_text(item) for item in value.values())
    if isinstance(value, list):
        return "".join(_native_text(item) for item in value)
    return ""


def _with_passthrough_compaction(
    options: dict[str, Any], *, resolved: dict, protocol: Literal["anthropic", "responses"]
) -> dict[str, Any]:
    """Arm provider-native compaction on a compatibility request that left it unset.

    These surfaces carry no durable run, so Lumen's own context fence never sees
    them; the provider's own compaction is the only thing standing between a
    long client session and a hard context-length error. A caller that already
    sent ``context_management`` keeps its own configuration untouched.
    """
    settings = get_settings()
    enabled = settings.chat_native_compaction_enabled and settings.chat_native_compaction_passthrough_enabled
    resolver = (
        native_compaction.anthropic_passthrough_options
        if protocol == "anthropic"
        else native_compaction.responses_passthrough_options
    )
    return resolver(
        options,
        resolved=resolved,
        ratio=context_manager.COMPACTION_REQUIRED,
        enabled=enabled,
    )


def _native_usage(
    value: dict, *, protocol: Literal["anthropic", "responses"], strict: bool = False
) -> dict | None:
    """Map raw provider-native usage to the runtime dict ``_bill`` consumes.

    Anthropic ``input_tokens`` excludes cache, so cache read/creation are added
    to reach total input; Responses ``input_tokens`` already includes cached
    tokens, which must not be added again. Under provider-native compaction the
    top-level counters cover the non-compaction iterations only, so the
    per-iteration breakdown is authoritative whenever the provider sends one.
    """
    usage = value.get("usage")
    if not isinstance(usage, dict):
        if strict and usage is not None:
            return UsageBreakdown(0, 0, modality_usage_invalid=True).as_usage_dict()
        return None
    if strict:

        def malformed(value: Any, key: str = "") -> bool:
            if key.endswith("tokens"):
                return isinstance(value, bool) or not isinstance(value, int) or value < 0
            if key in {"input_tokens_details", "output_tokens_details", "cached_tokens_details", "cache_creation"} and not isinstance(value, dict):
                return True
            if isinstance(value, dict):
                return any(malformed(item, name) for name, item in value.items())
            if isinstance(value, list):
                return any(malformed(item) for item in value)
            return False

        invalid = malformed(usage)
        if protocol == "responses" and "input_tokens" in usage and "output_tokens" in usage:
            checked = UsageBreakdown.from_openai_media(usage)
            invalid = invalid or checked is None or checked.modality_usage_invalid
        if protocol == "anthropic" and not invalid:
            creation = usage.get("cache_creation_input_tokens")
            split = usage.get("cache_creation") or {}
            five = split.get("ephemeral_5m_input_tokens")
            one = split.get("ephemeral_1h_input_tokens")
            if creation is not None:
                invalid = (five or 0) + (one or 0) > creation
                if five is not None and one is not None:
                    invalid = invalid or five + one != creation
        if invalid:
            return UsageBreakdown(0, 0, modality_usage_invalid=True).as_usage_dict()
    breakdown = (
        UsageBreakdown.from_anthropic(usage) if protocol == "anthropic" else UsageBreakdown.from_responses(usage)
    )
    if strict and breakdown is None:
        return UsageBreakdown(0, 0, modality_usage_invalid=True).as_usage_dict()
    return breakdown.as_usage_dict() if breakdown is not None else None


def responses_input_messages(input_value: str | list[dict]) -> list[dict]:
    if isinstance(input_value, str):
        return [{"role": "user", "content": input_value}]
    return [item for item in input_value if isinstance(item, dict)]


def responses_provider_options(options: dict[str, Any], *, resolved: dict) -> dict[str, Any]:
    """Drop reasoning only for a model an operator/catalog explicitly marked unsupported."""
    reasoning = options.get("reasoning") or {}
    options = _openai_provider_options(options, resolved=resolved, effort=reasoning.get("effort"))
    if resolved.get("reasoning_unsupported") is True:
        options.pop("reasoning", None)
        if include := options.get("include"):
            filtered = [item for item in include if not item.startswith("reasoning.")]
            if filtered:
                options["include"] = filtered
            else:
                options.pop("include")
    return options


def responses_usage(payload: dict, *, strict: bool) -> tuple[str, dict | None]:
    """``(output_text, runtime_usage)`` of one nonstreaming Responses payload for billing."""
    return _native_text(payload.get("output", [])), _native_usage(payload, protocol="responses", strict=strict)


async def invoke_responses_once(
    *, resolved: dict, input: str | list[dict], options: dict[str, Any], provider_once: bool = False
) -> dict:
    """Send one nonstreaming native Responses request; returns the provider payload, no billing.

    ``options`` must already be the exact provider options. Provider exceptions
    propagate unchanged; ``provider_once`` disables every retry/re-send layer.
    """
    response = await provider_client.aresponses(
        model=resolved["model_name"],
        input=input,
        stream=False,
        api_base=resolved.get("api_base"),
        api_key=resolved.get("api_key"),
        custom_llm_provider=resolved.get("provider_type"),
        provider_auth=resolved.get("provider_auth"),
        **({"provider_once": True} if provider_once else {}),
        **options,
    )
    if _official_openai(resolved) and isinstance(response, dict):
        provider_client.check_openai_tier(response)
    return _native_dict(response)


def _raise_responses_error(exc: Exception, resolved: dict, hold: _OnlineReservation | None) -> NoReturn:
    """Map one Responses provider failure to the online compatibility error contract."""
    if isinstance(exc, CompletionError):
        raise exc
    if hold is not None and hold.dispatched and isinstance(exc, (OpenAIServiceTierMismatch, OpenAIUsageInvalid)):
        raise _unknown_provider_result(exc) from None
    if isinstance(exc, errors.ProviderSubscriptionError):
        raise CompletionError(exc.status_code, exc.message) from None
    if isinstance(exc, BadRequestError):
        logger.info("Responses provider rejected request model=%s", resolved.get("model_name"))
        raise CompletionError(400, "upstream model rejected request") from None
    logger.warning("Responses upstream request failed model=%s", resolved.get("model_name"), exc_info=exc)
    raise CompletionError(502, "upstream model error") from exc


@receipt_owner
async def complete_responses(
    *,
    resolved: dict,
    input: str | list[dict],
    stream: bool,
    user_id: str,
    project_id: str,
    api_key_id: int | None,
    payer: Payer | None = None,
    options: dict[str, Any],
) -> dict | AsyncIterator[dict]:
    """Execute the native Responses protocol and preserve every upstream item/event."""
    official = _official_openai(resolved)
    catalog = provider_metadata.native_catalog_entry(
        resolved["model_name"], "openai", resolved.get("api_base"),
    ) if official else {}
    options = responses_provider_options(
        _with_passthrough_compaction(options, resolved=resolved, protocol="responses"), resolved=resolved
    )
    try:
        meter = openai_tool_policy.admit(
            input, options, metered=catalog.get("id") in openai_tool_policy.METERED_MODELS,
        ) if official else None
    except openai_tool_policy.UnsupportedProviderReference:
        raise CompletionError(
            422, "References to provider-side objects are not supported.", code="provider_reference_unsupported",
        ) from None
    except openai_tool_policy.UnsupportedHostedTool:
        raise CompletionError(
            422, "Tool configuration uses unsupported provider-side state or fees.", code="hosted_tool_unsupported",
        ) from None
    messages = responses_input_messages(input)
    if options.get("instructions"):
        messages = [{"role": "system", "content": options["instructions"]}, *messages]
    resolved = billing_route(resolved, messages, protocol="responses", options=options)
    await _authorize_completion(user_id, project_id, api_key_id, options, resolved)
    max_output = options.get("max_output_tokens") or catalog.get("max_output_tokens") or _DEFAULT_MAX_TOKENS
    if payer is not None:
        options["max_output_tokens"] = max_output
    hold = await _reserve_online(
        resolved, messages=messages, tools=options.get("tools"),
        max_output=max_output,
        user_id=user_id, project_id=project_id, api_key_id=api_key_id,
        payer=payer,
        tool_bound_usd=meter.bound_usd if meter is not None else Decimal("0"), prompt_options=options,
    )
    event_id = hold.event_id if hold is not None else str(uuid.uuid4())

    async def settle(payload: dict, streamed_text: str = "") -> None:
        try:
            if hold is not None and payload.get("status") not in ("completed", "incomplete"):
                raise OpenAIUsageInvalid()
            if meter is not None:
                provider_client.check_openai_tier(payload)
                tool_charges = meter.charges(payload.get("output"))
            else:
                tool_charges = None
        except (OpenAIServiceTierMismatch, OpenAIUsageInvalid) as exc:
            if hold is not None and hold.dispatched:
                raise _unknown_provider_result(exc) from None
            raise CompletionError(502, "upstream model error") from None
        text, usage = responses_usage(payload, strict=hold is not None or bool(resolved.get("token_rates")))
        await _bill(
            resolved, messages, streamed_text or text, usage,
            event_id=event_id, user_id=user_id, project_id=project_id, api_key_id=api_key_id,
            hold=hold, tool_charges=tool_charges,
        )

    if not stream:
        try:
            if hold is not None:
                hold.dispatched = True
            payload = await invoke_responses_once(resolved=resolved, input=input, options=options)
            await settle(payload)
            return payload
        except Exception as exc:
            _provider_rejected(hold, exc)
            _raise_responses_error(exc, resolved, hold)
        finally:
            if hold is not None:
                await hold.aclose()
    try:
        if hold is not None:
            hold.dispatched = True
        response = await provider_client.aresponses(
            model=resolved["model_name"],
            input=input,
            stream=stream,
            api_base=resolved.get("api_base"),
            api_key=resolved.get("api_key"),
            custom_llm_provider=resolved.get("provider_type"),
            provider_auth=resolved.get("provider_auth"),
            **options,
        )
    except Exception as exc:
        if hold is not None:
            _provider_rejected(hold, exc)
            await hold.aclose()
        _raise_responses_error(exc, resolved, hold)
    except BaseException:
        if hold is not None:
            await hold.aclose()
        raise

    async def events() -> AsyncIterator[dict]:
        charged = False
        text_parts: list[str] = []
        settlement_attempted = False
        try:
            async for raw_event in response:
                event = _native_dict(raw_event)
                event_type = event.get("type")
                if event_type == "response.output_text.delta" and isinstance(event.get("delta"), str):
                    text_parts.append(event["delta"])
                if event_type == "response.completed" and isinstance(event.get("response"), dict):
                    settlement_attempted = True
                    await settle(event["response"], "".join(text_parts))
                    charged = True
                yield event
        except (OpenAIServiceTierMismatch, OpenAIUsageInvalid) as exc:
            text_parts.clear()
            if hold is not None and hold.dispatched:
                raise _unknown_provider_result(exc) from None
            raise CompletionError(502, "upstream model error") from None
        finally:
            # Stop upstream generation first; close failures are logged, never skip billing.
            await _close_provider_stream(response)
            if hold is not None:
                await hold.aclose()
            elif (
                not charged and text_parts and not resolved.get("required_token_modalities")
                and not (settlement_attempted and (official or resolved.get("token_rates")))
            ):
                try:
                    await _bill(
                        resolved,
                        messages,
                        "".join(text_parts),
                        None,
                        event_id=event_id,
                        user_id=user_id,
                        project_id=project_id,
                        api_key_id=api_key_id,
                    )
                except Exception:
                    logger.warning("Responses partial-stream accounting failed", exc_info=True)

    return _ProviderEvents(events(), response, hold)


@receipt_owner
async def complete_anthropic(
    *,
    resolved: dict,
    messages: list[dict],
    max_tokens: int,
    stream: bool,
    user_id: str,
    project_id: str,
    api_key_id: int | None,
    payer: Payer | None = None,
    options: dict[str, Any],
) -> dict | AsyncIterator[dict]:
    """Execute the native Anthropic protocol without lossy OpenAI conversion."""
    resolved = billing_route(resolved, messages, protocol="anthropic", options=options)
    options = _with_passthrough_compaction(options, resolved=resolved, protocol="anthropic")
    await _authorize_completion(user_id, project_id, api_key_id, options, resolved)
    hold = await _reserve_online(
        resolved, messages=messages, tools=options.get("tools"), max_output=max_tokens,
        user_id=user_id, project_id=project_id, api_key_id=api_key_id, payer=payer, prompt_options=options,
    ) if payer is not None else None
    event_id = hold.event_id if hold else str(uuid.uuid4())
    try:
        if hold is not None:
            hold.dispatched = True
        response = await provider_client.aanthropic_messages(
            model=resolved["model_name"],
            messages=messages,
            max_tokens=max_tokens,
            stream=stream,
            api_base=resolved.get("api_base"),
            api_key=resolved.get("api_key"),
            custom_llm_provider=resolved.get("provider_type"),
            provider_auth=resolved.get("provider_auth"),
            **options,
        )
    except errors.ProviderSubscriptionError as exc:
        _provider_rejected(hold, exc)
        if hold is not None:
            await hold.aclose()
        raise CompletionError(exc.status_code, exc.message) from None
    except BaseException as exc:
        _provider_rejected(hold, exc)
        if hold is not None:
            await hold.aclose()
        if not isinstance(exc, Exception):
            raise
        logger.warning("Anthropic upstream request failed model=%s", resolved.get("model_name"), exc_info=True)
        raise CompletionError(502, "upstream model error") from exc

    if not stream:
        try:
            payload = _native_dict(response)
            await _bill(
                resolved,
                messages,
                _native_text(payload.get("content", [])),
                _native_usage(payload, protocol="anthropic", strict=bool(resolved.get("token_rates"))),
                event_id=event_id,
                user_id=user_id,
                project_id=project_id,
                api_key_id=api_key_id,
                hold=hold,
            )
            return payload
        finally:
            if hold is not None:
                await hold.aclose()

    # Start the provider chain before HTTP 200: LiteLLM's Anthropic byte stream
    # releases its pooled connection on close only after it was iterated.
    upstream = aiter(response)
    try:
        first_event = await anext(upstream)
    except StopAsyncIteration:
        first_event = None
    except BaseException as exc:
        # The ASGI resource owner is registered only after this function returns.
        with anyio.CancelScope(shield=True):
            await _close_provider_stream(upstream)
            _provider_rejected(hold, exc)
            if hold is not None:
                await hold.aclose()
        if isinstance(exc, errors.ProviderSubscriptionError):
            raise CompletionError(exc.status_code, exc.message) from None
        if not isinstance(exc, Exception):
            raise
        logger.warning("Anthropic upstream stream failed model=%s", resolved.get("model_name"), exc_info=True)
        raise CompletionError(502, "upstream model error") from exc

    async def raw_events() -> AsyncIterator[Any]:
        if first_event is not None:
            yield first_event
        async for item in upstream:
            yield item

    async def events() -> AsyncIterator[dict]:
        start_usage: dict = {}
        delta_usages: list[dict] = []
        text_parts: list[str] = []
        charged = False
        settlement_attempted = False
        try:
            async for raw_event in raw_events():
                event = _native_dict(raw_event)
                event_type = event.get("type")
                if event_type == "message_start":
                    usage = (event.get("message") or {}).get("usage")
                    start_usage = usage if isinstance(usage, dict) else {}
                elif event_type == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                        text_parts.append(delta["text"])
                elif event_type == "message_delta":
                    usage = event.get("usage")
                    # message_delta usage is cumulative: later fields override
                    # message_start ones. A compacted turn reports its real
                    # cost only in the per-iteration breakdown, which the
                    # merged breakdown prefers over the top-level counters.
                    if isinstance(usage, dict):
                        delta_usages.append(usage)
                elif event_type == "message_stop":
                    settlement_attempted = True
                    merged_usage = {"input_tokens": 0, "output_tokens": 0}
                    for usage in (start_usage, *delta_usages):
                        merged_usage.update({key: value for key, value in usage.items() if value is not None})
                    runtime_usage = _native_usage(
                        {"usage": merged_usage}, protocol="anthropic", strict=bool(resolved.get("token_rates"))
                    )
                    await _bill(
                        resolved,
                        messages,
                        "".join(text_parts),
                        runtime_usage,
                        event_id=event_id,
                        user_id=user_id,
                        project_id=project_id,
                        api_key_id=api_key_id,
                        hold=hold,
                    )
                    charged = True
                yield event
        finally:
            # Stop upstream generation first; close failures are logged, never skip billing.
            await _close_provider_stream(response)
            if (
                hold is None and not charged and text_parts and not resolved.get("required_token_modalities")
                and not (resolved.get("token_rates") and settlement_attempted)
            ):
                try:
                    completion_text = "".join(text_parts)
                    # A stream cut before message_stop still bills what
                    # message_start reported: its input side and cache split
                    # stay authoritative, and the output falls back to the
                    # local counter only when that exceeds the reported count.
                    # LiteLLM's non-Anthropic /v1/messages adapters open with an
                    # all-zero usage and report real counts only at the end, so
                    # a report with no input at all is not evidence of a free
                    # prompt: the prompt is counted locally instead.
                    reported = (
                        UsageBreakdown.from_anthropic_stream(start_usage, delta_usages)
                        if start_usage or delta_usages
                        else None
                    )
                    if resolved.get("token_rates") and (start_usage or delta_usages):
                        merged_usage = {"input_tokens": 0, "output_tokens": 0}
                        for usage in (start_usage, *delta_usages):
                            merged_usage.update({key: value for key, value in usage.items() if value is not None})
                        runtime_usage = _native_usage({"usage": merged_usage}, protocol="anthropic", strict=True)
                        reported = UsageBreakdown.from_runtime(runtime_usage)
                        if reported is None or reported.modality_usage_invalid:
                            raise CompletionError(502, "modality_usage_unavailable")
                    output_tokens = max(
                        reported.output_tokens if reported is not None else 0,
                        provider_metadata.count_tokens(resolved["model_name"], text=completion_text),
                    )
                    if reported is not None and reported.input_tokens > 0:
                        partial = replace(reported, output_tokens=output_tokens)
                    else:
                        partial = UsageBreakdown.from_totals(
                            provider_metadata.count_tokens(resolved["model_name"], messages=messages),
                            output_tokens,
                        )
                    await _bill(
                        resolved,
                        messages,
                        completion_text,
                        partial.as_usage_dict(),
                        event_id=event_id,
                        user_id=user_id,
                        project_id=project_id,
                        api_key_id=api_key_id,
                    )
                except Exception:
                    logger.warning("Anthropic partial-stream accounting failed", exc_info=True)

    return _ProviderEvents(events(), upstream, hold)


async def _bill_gemini(
    resolved: dict, messages: list[dict], usage: Any, *,
    user_id: str, project_id: str, api_key_id: int | None, hold: _OnlineReservation | None,
) -> None:
    breakdown = UsageBreakdown.from_gemini(usage)
    if breakdown is None or breakdown.modality_usage_invalid:
        raise CompletionError(502, "gemini_usage_invalid", code="provider_result_unknown")
    await _bill(
        resolved, messages, "", breakdown.as_usage_dict(),
        event_id=hold.event_id if hold else str(uuid.uuid4()), user_id=user_id, project_id=project_id,
        api_key_id=api_key_id, hold=hold,
    )


@receipt_owner
async def complete_gemini(
    *, resolved: dict, payload: dict, messages: list[dict], max_tokens: int, stream: bool,
    user_id: str, project_id: str, api_key_id: int | None, payer: Payer | None = None,
) -> dict | AsyncIterator[dict]:
    """Keep native Gemini parts/usage intact while using the normal stateless ledger."""
    resolved = billing_route(resolved, messages, protocol="gemini")
    # Only client functions are allowed in this closed request. Intrinsic server
    # tools still require their own scope even when no tool declarations are sent.
    await _authorize_completion(user_id, project_id, api_key_id, {}, resolved)
    hold = await _reserve_online(
        resolved, messages=messages, tools=None, max_output=max_tokens, user_id=user_id,
        project_id=project_id, api_key_id=api_key_id, payer=payer,
    )
    try:
        if hold is not None:
            hold.dispatched = True
        response = await provider_client.agemini_generate(
            model=resolved["model_name"], payload=payload, stream=stream,
            api_base=resolved.get("api_base"), api_key=resolved.get("api_key"),
            provider_auth=resolved.get("provider_auth"),
        )
        if not stream:
            await _bill_gemini(
                resolved, messages, response.get("usageMetadata"), user_id=user_id,
                project_id=project_id, api_key_id=api_key_id, hold=hold,
            )
            return response
    except BaseException as exc:
        if isinstance(exc, Exception):
            _provider_rejected(hold, exc)
        if hold is not None:
            await hold.aclose()
        raise
    finally:
        if not stream and hold is not None:
            await hold.aclose()

    async def events() -> AsyncIterator[dict]:
        usage = None
        try:
            async for frame in response:
                if "usageMetadata" in frame:
                    usage = frame.pop("usageMetadata")
                if frame:
                    yield frame
            await _bill_gemini(
                resolved, messages, usage, user_id=user_id, project_id=project_id, api_key_id=api_key_id, hold=hold,
            )
            yield {"usageMetadata": usage}
        finally:
            if hold is not None:
                await hold.aclose()

    return _ProviderEvents(events(), response, hold)


async def count_anthropic_tokens(
    *,
    resolved: dict,
    payload: dict[str, Any],
    anthropic_headers: dict[str, str] | None = None,
) -> dict[str, int]:
    """Use Anthropic's authoritative count endpoint or fail explicitly when unavailable."""
    if resolved.get("provider_type") != "anthropic":
        raise CompletionError(501, "token_count_unavailable")
    from lumen.services.providers.credentials import resolve_inference_credential, transport_model_name

    auth = resolved.get("provider_auth")
    if auth is not None and auth.get("auth_mode") != "api_key":
        raise CompletionError(501, "token_count_unavailable")
    credential = await resolve_inference_credential(auth) if auth is not None else resolved
    api_key = credential.get("api_key")
    if not isinstance(api_key, str) or not api_key:
        raise CompletionError(501, "token_count_unavailable")
    base = str(resolved.get("api_base") or "https://api.anthropic.com").rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    body = dict(payload)
    body["model"] = transport_model_name(resolved["model_name"])
    try:
        import httpx

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False, trust_env=False) as client:
            response = await client.post(
                f"{base}/messages/count_tokens",
                headers={
                    "x-api-key": api_key,
                    "content-type": "application/json",
                    "anthropic-version": "2023-06-01",
                    **(anthropic_headers or {}),
                },
                json=body,
            )
    except Exception as exc:
        raise CompletionError(503, "token_count_unavailable") from exc
    if response.status_code == 429:
        raise CompletionError(429, "rate_limited")
    if response.status_code >= 500:
        raise CompletionError(503, "token_count_unavailable")
    if response.status_code >= 400:
        raise CompletionError(400, "token_count_request_invalid")
    try:
        count = int(response.json()["input_tokens"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CompletionError(502, "token_count_unavailable") from exc
    return {"input_tokens": count}
