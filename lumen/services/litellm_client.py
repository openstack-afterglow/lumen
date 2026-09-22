"""litellm 호출 래퍼 + 비용/토큰 계산.

핵심 책임:
- provider 설정(api_base/api_key)을 주입해 litellm 를 호출(스트리밍/비스트리밍).
- ⚠️ 스트리밍 응답은 기본적으로 usage 를 주지 않으므로 `stream_options={"include_usage": True}`
  로 요청하고, 그래도 없으면 `litellm.token_counter` 로 폴백 산출한다(과금 0원 방지).
- 토큰 수 → USD 비용 산출(litellm 내장 가격표, `cost_per_token`).

litellm 은 무거운 import 라 모든 함수 내부에서 lazy import 한다(startup 속도 유지).
litellm 의 로컬 계산(token_counter/cost_per_token)만 쓰는 함수는 네트워크가 필요 없다.
"""

from __future__ import annotations

import codecs
import json
import logging
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import urlsplit

from lumen.services.providers.credentials import (
    ProviderAuthRef,
    api_model_name,
    litellm_model_name,
    perplexity_route_model_name,
)
from lumen.services.providers.errors import ProviderSubscriptionError

logger = logging.getLogger(__name__)

_RAW_COST_QUANTUM = Decimal("0.0000000001")
_TOKENS_PER_MILLION = Decimal("1000000")

# Exact token rates published by Perplexity Agent API that are absent from the
# pinned LiteLLM catalog. This is deliberately not a model-family fallback.
_OFFICIAL_PRICE_PER_MILLION: dict[tuple[str, str], tuple[Decimal, Decimal, str]] = {
    ("perplexity", "perplexity/sonar"): (
        Decimal("0.25"),
        Decimal("2.50"),
        "perplexity_agent_api_2026-09",
    ),
    ("perplexity", "perplexity/glm-5.3"): (
        Decimal("1.40"),
        Decimal("4.40"),
        "perplexity_agent_api_2026-09",
    ),
}


@dataclass(frozen=True)
class UsageCost:
    raw_cost: Decimal
    input_cost: Decimal
    output_cost: Decimal
    pricing_status: Literal["priced", "partial", "unpriced"]
    pricing_snapshot: dict[str, object]


@dataclass(frozen=True)
class ContextTokenCount:
    """Context accounting result; ``unknown`` is never presented as zero."""

    tokens: int | None
    measurement: Literal["tokenizer", "estimated", "unknown"]
    tokenizer: str | None = None
    reason_code: str | None = None


def count_tokens(model: str, *, messages: list[dict] | None = None, text: str | None = None) -> int:
    """litellm.token_counter 로 토큰 수 산출. 실패 시 대략치(4 chars ≈ 1 token) 폴백."""
    normalized_model = litellm_model_name(model)
    try:
        import litellm

        if messages is not None:
            return int(litellm.token_counter(model=normalized_model, messages=messages))
        return int(litellm.token_counter(model=normalized_model, text=text or ""))
    except Exception:
        logger.warning("litellm token_counter 실패 model=%s — 대략치 폴백", model, exc_info=True)
        raw = text if text is not None else "".join(str(m.get("content", "")) for m in (messages or []))
        return max(1, len(raw) // 4)


def _contains_uncountable_modality(value: object) -> bool:
    """Reject modalities whose provider representation cannot be measured locally."""
    if isinstance(value, dict):
        kind = value.get("type")
        if kind in {"audio", "video", "file"}:
            return True
        return any(_contains_uncountable_modality(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_uncountable_modality(item) for item in value)
    return False


def _contains_multimodal(value: object) -> bool:
    """Return true for image/provider content whose local count is only an estimate."""
    if isinstance(value, dict):
        if value.get("type") in {"image_url", "audio", "video", "file"}:
            return True
        return any(_contains_multimodal(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_multimodal(item) for item in value)
    return False


def count_context_tokens(model: str, messages: list[dict], tools: list[dict] | None = None) -> ContextTokenCount:
    """Count the complete provider context without network-fetching media.

    Unlike :func:`count_tokens`, this API carries measurement provenance and
    never turns an unsupported modality into a fabricated token count.
    """
    tools = tools or []
    if _contains_uncountable_modality(messages) or _contains_uncountable_modality(tools):
        return ContextTokenCount(tokens=None, measurement="unknown", reason_code="token_count_unavailable")
    multimodal = _contains_multimodal(messages) or _contains_multimodal(tools)
    normalized_model = litellm_model_name(model)
    try:
        import litellm

        count = int(
            litellm.token_counter(
                model=normalized_model,
                messages=messages,
                tools=tools,
                use_default_image_token_count=True,
            )
        )
        tokenizer = None
        try:
            optional = litellm.get_optional_params(model=normalized_model)
            tokenizer = str(optional.get("tokenizer") or "") or None if isinstance(optional, Mapping) else None
        except Exception:
            tokenizer = None
        if multimodal or tokenizer is None:
            return ContextTokenCount(tokens=max(0, count), measurement="estimated", tokenizer=tokenizer)
        return ContextTokenCount(tokens=max(0, count), measurement="tokenizer", tokenizer=tokenizer)
    except Exception:
        raw = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False, sort_keys=True, default=str)
        if multimodal:
            return ContextTokenCount(tokens=None, measurement="unknown", reason_code="token_count_unavailable")
        return ContextTokenCount(
            tokens=max(1, (len(raw) + 3) // 4),
            measurement="estimated",
            reason_code="token_counter_failed",
        )


def _usage_field(usage: Any, key: str) -> int | None:
    """usage(dict 또는 litellm Usage 객체)에서 정수 필드를 안전하게 추출."""
    val = usage.get(key) if isinstance(usage, Mapping) else getattr(usage, key, None)
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def extract_usage(
    model: str,
    messages: list[dict],
    completion_text: str,
    final_usage: Any | None,
) -> tuple[int, int]:
    """(prompt_tokens, completion_tokens) 산출.

    스트리밍 마지막 청크의 usage 가 있으면 그대로, 없으면 token_counter 폴백.
    이 폴백이 없으면 스트리밍 경로에서 completion_cost 가 0이 되어 과금이 누락된다.
    """
    if final_usage is not None:
        pt = _usage_field(final_usage, "prompt_tokens")
        ct = _usage_field(final_usage, "completion_tokens")
        if pt is not None and ct is not None:
            return pt, ct

    prompt_tokens = count_tokens(model, messages=messages)
    completion_tokens = count_tokens(model, text=completion_text)
    return prompt_tokens, completion_tokens


def _as_decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


def chatgpt_model_metadata(model_name: str) -> dict[str, Any]:
    """Read one exact bundled ChatGPT entry without provider/authentication probes."""
    import litellm

    bare_model = litellm_model_name(model_name)
    metadata = litellm.model_cost.get(f"chatgpt/{bare_model}")
    if not isinstance(metadata, dict) or metadata.get("litellm_provider") != "chatgpt":
        return {}
    return dict(metadata)


def _pricing_model_candidates(model: str, provider_type: str | None) -> tuple[str, ...]:
    normalized_model = litellm_model_name(model)
    candidates = [normalized_model]
    if provider_type == "perplexity":
        canonical_model = api_model_name(normalized_model, "perplexity")
        if canonical_model not in candidates:
            candidates.append(canonical_model)
    return tuple(candidates)


def _litellm_component_rates(
    model: str, prompt_tokens: int, completion_tokens: int, provider_type: str | None = None
) -> tuple[Decimal | None, Decimal | None]:
    if provider_type == "chatgpt" or model.startswith("chatgpt/"):
        metadata = chatgpt_model_metadata(model)
        return (
            _as_decimal(metadata.get("input_cost_per_token")),
            _as_decimal(metadata.get("output_cost_per_token")),
        )
    lookup_prompt_tokens = max(1, prompt_tokens)
    lookup_completion_tokens = max(1, completion_tokens)
    try:
        import litellm
    except Exception:
        logger.warning("litellm cost catalog is unavailable model=%s", model, exc_info=True)
        return None, None
    for candidate in _pricing_model_candidates(model, provider_type):
        kwargs = {
            "model": candidate,
            "prompt_tokens": lookup_prompt_tokens,
            "completion_tokens": lookup_completion_tokens,
        }
        if provider_type:
            kwargs["custom_llm_provider"] = provider_type
        try:
            prompt_cost, completion_cost = litellm.cost_per_token(**kwargs)
        except Exception:
            continue
        prompt_total = _as_decimal(prompt_cost)
        completion_total = _as_decimal(completion_cost)
        if prompt_total is not None and completion_total is not None:
            return prompt_total / lookup_prompt_tokens, completion_total / lookup_completion_tokens
    return None, None


def _official_component_rates(
    model: str, provider_type: str | None, api_base: str | None = None
) -> tuple[Decimal | None, Decimal | None, str | None]:
    if provider_type != "perplexity" or _perplexity_mode(model, api_base) != "agent":
        return None, None, None
    canonical_model = api_model_name(litellm_model_name(model), "perplexity")
    published = _OFFICIAL_PRICE_PER_MILLION.get((provider_type, canonical_model))
    if published is None:
        return None, None, None
    input_per_million, output_per_million, source = published
    return input_per_million / _TOKENS_PER_MILLION, output_per_million / _TOKENS_PER_MILLION, source


def _fallback_component_rates(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    provider_type: str | None = None,
    api_base: str | None = None,
) -> tuple[Decimal | None, Decimal | None, str | None]:
    # Agent Sonar is a different priced product from the legacy Sonar API.
    # Exact published Agent rates must win over its legacy bundled alias.
    official_input, official_output, official_source = _official_component_rates(model, provider_type, api_base)
    if official_input is not None and official_output is not None:
        return official_input, official_output, official_source
    input_rate, output_rate = _litellm_component_rates(model, prompt_tokens, completion_tokens, provider_type)
    return input_rate, output_rate, "litellm" if input_rate is not None or output_rate is not None else None


def _component_cost(
    *,
    tokens: int,
    stored_rate: Decimal | None,
    stored_source: str | None,
    fallback_rate: Decimal | None,
    fallback_source: str | None,
) -> tuple[Decimal, Decimal | None, str | None, bool]:
    if stored_rate is not None:
        return stored_rate * tokens, stored_rate, stored_source, True
    if fallback_rate is not None:
        return fallback_rate * tokens, fallback_rate, fallback_source, True
    return Decimal("0"), None, None, False


def effective_prices_per_million(
    model: str, provider_type: str | None = None, *, api_base: str | None = None
) -> tuple[Decimal | None, Decimal | None]:
    """Return exact token prices for the configured API route, never a family alias."""
    input_rate, output_rate, _ = _fallback_component_rates(model, 1_000_000, 1_000_000, provider_type, api_base)

    def display_price(rate: Decimal | None) -> Decimal | None:
        return (
            (rate * _TOKENS_PER_MILLION).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
            if rate is not None
            else None
        )

    return display_price(input_rate), display_price(output_rate)


def official_price_source(model: str, provider_type: str | None = None, *, api_base: str | None = None) -> str | None:
    """Return provenance for an exact documented route price, never a guessed rate."""
    _, _, source = _official_component_rates(model, provider_type, api_base)
    return source


def _decimal_string(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def cost_from_usage(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    input_price_per_token: Decimal | None,
    output_price_per_token: Decimal | None,
    price_source: str | None,
    provider_type: str | None = None,
) -> UsageCost:
    """Resolve manual → reviewed models.dev → LiteLLM bundled pricing per component."""
    prompt_tokens = max(0, int(prompt_tokens))
    completion_tokens = max(0, int(completion_tokens))
    fallback_input, fallback_output, fallback_source = (
        _fallback_component_rates(model, prompt_tokens, completion_tokens, provider_type)
        if input_price_per_token is None or output_price_per_token is None
        else (None, None, None)
    )
    input_cost, input_rate, input_source, input_priced = _component_cost(
        tokens=prompt_tokens,
        stored_rate=input_price_per_token,
        stored_source=price_source,
        fallback_rate=fallback_input,
        fallback_source=fallback_source,
    )
    output_cost, output_rate, output_source, output_priced = _component_cost(
        tokens=completion_tokens,
        stored_rate=output_price_per_token,
        stored_source=price_source,
        fallback_rate=fallback_output,
        fallback_source=fallback_source,
    )
    priced_components = int(input_priced) + int(output_priced)
    pricing_status: Literal["priced", "partial", "unpriced"]
    if priced_components == 2:
        pricing_status = "priced"
    elif priced_components:
        pricing_status = "partial"
    else:
        pricing_status = "unpriced"
    raw_cost = (input_cost + output_cost).quantize(_RAW_COST_QUANTUM, rounding=ROUND_HALF_UP)
    snapshot = {
        "model": model,
        "input": {
            "tokens": prompt_tokens,
            "effective_price_per_token": _decimal_string(input_rate),
            "effective_price_per_million": _decimal_string(
                input_rate * _TOKENS_PER_MILLION if input_rate is not None else None
            ),
            "cost": _decimal_string(input_cost.quantize(_RAW_COST_QUANTUM, rounding=ROUND_HALF_UP)),
            "source": input_source,
            "provider_type": provider_type,
        },
        "output": {
            "tokens": completion_tokens,
            "effective_price_per_token": _decimal_string(output_rate),
            "effective_price_per_million": _decimal_string(
                output_rate * _TOKENS_PER_MILLION if output_rate is not None else None
            ),
            "cost": _decimal_string(output_cost.quantize(_RAW_COST_QUANTUM, rounding=ROUND_HALF_UP)),
            "source": output_source,
        },
    }
    return UsageCost(
        raw_cost=raw_cost,
        input_cost=input_cost.quantize(_RAW_COST_QUANTUM, rounding=ROUND_HALF_UP),
        output_cost=output_cost.quantize(_RAW_COST_QUANTUM, rounding=ROUND_HALF_UP),
        pricing_status=pricing_status,
        pricing_snapshot=snapshot,
    )


_VALID_EFFORTS = {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
_OMIT_EFFORTS = {"", "auto", "off", "disabled", "false"}


def _reasoning_params(model: str, effort: str | None, custom_llm_provider: str | None) -> dict[str, Any]:
    """Attach reasoning only when side-effect-free metadata or provider probes support it."""
    if effort is None:
        return {}
    normalized = effort.strip().lower()
    if normalized in _OMIT_EFFORTS:
        return {}
    if normalized != "none" and normalized not in _VALID_EFFORTS:
        return {}
    normalized_model = litellm_model_name(model)
    if custom_llm_provider == "chatgpt" or model.startswith("chatgpt/"):
        supported = bool(chatgpt_model_metadata(model).get("supports_reasoning"))
    else:
        try:
            import litellm

            supported = bool(litellm.supports_reasoning(model=normalized_model))
        except Exception:
            logger.warning("litellm supports_reasoning 조회 실패 model=%s", model, exc_info=True)
            return {}
    if not supported:
        return {}
    return {"reasoning_effort": normalized}


def _native_web_search_extra(
    extra: dict | None, native_web_search: dict[str, Any] | None, custom_llm_provider: str | None
) -> dict[str, Any] | None:
    if native_web_search is None:
        return extra
    merged = {**(extra or {}), "web_search_options": dict(native_web_search)}
    # Gemini drops server-side search when mixed with function declarations
    # unless this LiteLLM transport option is explicit.
    if custom_llm_provider == "gemini":
        merged["include_server_side_tool_invocations"] = True
    return merged


def _build_params(
    model: str,
    messages: list[dict],
    *,
    api_base: str | None,
    api_key: str | None,
    max_tokens: int | None,
    temperature: float | None,
    custom_llm_provider: str | None = None,
    tools: list[dict] | None = None,
    extra: dict | None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"model": model, "messages": messages}
    if custom_llm_provider:
        params["custom_llm_provider"] = custom_llm_provider
    if api_base:
        params["api_base"] = api_base
    if api_key:
        params["api_key"] = api_key
    if max_tokens is not None:
        params["max_tokens"] = max_tokens
    if temperature is not None:
        params["temperature"] = temperature
    if tools:
        params["tools"] = tools
    if extra:
        params.update(extra)
    return params


def _subscription_error(error: BaseException) -> ProviderSubscriptionError:
    status_code = getattr(error, "status_code", None)
    if status_code is None:
        status_code = getattr(getattr(error, "response", None), "status_code", None)
    if status_code in {401, 403}:
        return ProviderSubscriptionError("subscription_auth_required", 502)
    if status_code == 429:
        return ProviderSubscriptionError("subscription_rate_limited", 429)
    if isinstance(status_code, int) and status_code >= 500:
        return ProviderSubscriptionError("subscription_upstream_unavailable", 503)
    return ProviderSubscriptionError("subscription_auth_invalid_response", 502)


def _requires_subscription_auth(model: str, custom_llm_provider: str | None) -> bool:
    return custom_llm_provider == "chatgpt" or model.startswith(("chatgpt/", "anthropic-subscription/"))


async def _guard_subscription_stream(
    source: Any,
    provider_auth: ProviderAuthRef,
    fingerprint: str | None,
):
    from lumen.services.providers import subscriptions

    try:
        async for chunk in source:
            yield chunk
    except ProviderSubscriptionError as error:
        if error.code == "subscription_auth_required" and isinstance(fingerprint, str):
            await subscriptions._mark_subscription_credential_rejected(provider_auth, fingerprint)
        raise
    except BaseException as error:
        safe_error = _subscription_error(error)
        if safe_error.code == "subscription_auth_required" and isinstance(fingerprint, str):
            await subscriptions._mark_subscription_credential_rejected(provider_auth, fingerprint)
        raise safe_error from None
    finally:
        close = getattr(source, "aclose", None)
        if callable(close):
            await close()


def _next_sse_block(buffer: str) -> tuple[str, str] | None:
    boundaries = ((buffer.find("\r\n\r\n"), 4), (buffer.find("\n\n"), 2), (buffer.find("\r\r"), 2))
    candidates = [(index, width) for index, width in boundaries if index >= 0]
    if not candidates:
        return None
    index, width = min(candidates, key=lambda item: item[0])
    return buffer[:index], buffer[index + width :]


def _anthropic_sse_payload(block: str) -> dict[str, Any] | None:
    data_lines: list[str] = []
    for line in block.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line.startswith("data:"):
            continue
        value = line[5:]
        data_lines.append(value[1:] if value.startswith(" ") else value)
    if not data_lines:
        return None
    payload = json.loads("\n".join(data_lines))
    if not isinstance(payload, dict):
        raise ValueError("Anthropic SSE data must be a JSON object")
    return payload


async def _anthropic_stream_events(source: Any) -> AsyncIterator[Any]:
    """Normalize LiteLLM's raw native SSE byte chunks into Anthropic event objects."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    buffer = ""
    saw_bytes = False

    try:
        async for chunk in source:
            if isinstance(chunk, (bytes, bytearray, memoryview)):
                saw_bytes = True
                buffer += decoder.decode(bytes(chunk), final=False)
            elif isinstance(chunk, str):
                buffer += chunk
            else:
                if buffer.strip():
                    raise ValueError("Anthropic stream mixed incomplete SSE data with structured events")
                yield chunk
                continue

            while (split := _next_sse_block(buffer)) is not None:
                block, buffer = split
                payload = _anthropic_sse_payload(block)
                if payload is not None:
                    yield payload

        if saw_bytes:
            buffer += decoder.decode(b"", final=True)
        while (split := _next_sse_block(buffer)) is not None:
            block, buffer = split
            payload = _anthropic_sse_payload(block)
            if payload is not None:
                yield payload
        if buffer.strip():
            payload = _anthropic_sse_payload(buffer)
            if payload is not None:
                yield payload
    finally:
        close = getattr(source, "aclose", None)
        if callable(close):
            await close()


async def _subscription_completion(
    model: str,
    messages: list[dict],
    *,
    provider_auth: ProviderAuthRef,
    stream: bool,
    max_tokens: int | None,
    temperature: float | None,
    custom_llm_provider: str | None,
    tools: list[dict] | None,
    extra: dict | None,
) -> Any:
    import litellm

    from lumen.services.providers import subscriptions
    from lumen.services.providers.subscription_logging import SubscriptionLogging

    mode = provider_auth.get("auth_mode")
    expected_provider = "chatgpt" if mode == "chatgpt_device" else "anthropic"
    if mode not in {"chatgpt_device", "anthropic_subscription"} or custom_llm_provider != expected_provider:
        raise ProviderSubscriptionError("subscription_auth_required", 502)
    credential = await subscriptions.resolve_subscription_credential(provider_auth)
    fingerprint = credential.get("_fingerprint")
    optional_params = dict(extra or {})
    if max_tokens is not None:
        optional_params["max_tokens"] = max_tokens
    if temperature is not None:
        optional_params["temperature"] = temperature
    if tools:
        optional_params["tools"] = tools

    try:
        if mode == "chatgpt_device":
            from lumen.services.providers.chatgpt_transport import acompletion as chatgpt_acompletion

            result = await chatgpt_acompletion(
                model,
                messages,
                credential=credential,
                stream=stream,
                optional_params=optional_params,
            )
            if stream:
                return _guard_subscription_stream(result, provider_auth, fingerprint)
            return result

        access_token = credential.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ProviderSubscriptionError("subscription_auth_required", 502)
        normalized_model = litellm_model_name(model)
        logging_obj = SubscriptionLogging(
            model=normalized_model,
            provider="anthropic",
            fixed_api_base="https://api.anthropic.com",
            call_id=str(uuid.uuid4()),
            stream=stream,
        )
        params = _build_params(
            normalized_model,
            messages,
            api_base=None,
            api_key=None,
            max_tokens=max_tokens,
            temperature=temperature,
            custom_llm_provider="anthropic",
            tools=tools,
            extra={
                key: value
                for key, value in optional_params.items()
                if key
                not in {
                    "api_base",
                    "api_key",
                    "base_url",
                    "custom_llm_provider",
                    "litellm_logging_obj",
                    "model",
                    "messages",
                }
            },
        )
        params.update(
            {
                "model": normalized_model,
                "custom_llm_provider": "anthropic",
                "api_base": "https://api.anthropic.com",
                "api_key": access_token,
                "litellm_logging_obj": logging_obj,
                "stream": stream,
            }
        )
        if stream:
            params["stream_options"] = {"include_usage": True}
        result = await litellm.acompletion(**params)
        if stream:
            return _guard_subscription_stream(result, provider_auth, fingerprint)
        return result
    except ProviderSubscriptionError as error:
        if error.code == "subscription_auth_required" and isinstance(fingerprint, str):
            await subscriptions._mark_subscription_credential_rejected(provider_auth, fingerprint)
        raise
    except BaseException as error:
        safe_error = _subscription_error(error)
        if safe_error.code == "subscription_auth_required" and isinstance(fingerprint, str):
            await subscriptions._mark_subscription_credential_rejected(provider_auth, fingerprint)
        raise safe_error from None


def _perplexity_mode(model: str, api_base: str | None) -> Literal["router", "agent", "legacy"]:
    base_path = urlsplit(api_base or "").path.rstrip("/").lower()
    if base_path.endswith("/router") or base_path.endswith("/router/v1"):
        return "router"
    if base_path.endswith("/v1"):
        return "agent"
    if model.startswith("perplexity/") and model.count("/") >= 2:
        return "agent"
    if "/" in model and not model.startswith("perplexity/"):
        return "agent"
    return "legacy"


def _perplexity_agent_base(api_base: str | None) -> str:
    base = (api_base or "https://api.perplexity.ai").rstrip("/")
    if urlsplit(base).path.lower().endswith("/v1"):
        return base[:-3]
    return base


def _perplexity_router_base(api_base: str | None) -> str:
    base = (api_base or "").rstrip("/")
    if urlsplit(base).path.lower().endswith("/router"):
        return f"{base}/v1"
    return base


def _responses_output_item_annotations(items: Any) -> list[dict]:
    """Collect message-content annotations from Responses output items."""
    annotations: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            annotations.extend(entry for entry in part.get("annotations") or [] if isinstance(entry, dict))
    return annotations


def _responses_event_annotations(chunk: Any) -> list[dict]:
    """Read the citation annotations carried by one raw Responses stream event."""
    event = chunk.get("type") if isinstance(chunk, dict) else getattr(chunk, "type", None)
    event = getattr(event, "value", event)
    if event not in (
        "response.output_text.annotation.added",
        "response.output_text.done",
        "response.output_item.done",
        "response.completed",
    ):
        return []
    if hasattr(chunk, "model_dump"):
        chunk = chunk.model_dump()
    if not isinstance(chunk, dict):
        return []
    if event == "response.output_text.annotation.added":
        annotation = chunk.get("annotation")
        return [annotation] if isinstance(annotation, dict) else []
    if event == "response.output_text.done":
        return [entry for entry in chunk.get("annotations") or [] if isinstance(entry, dict)]
    if event == "response.output_item.done":
        return _responses_output_item_annotations([chunk.get("item")])
    if event == "response.completed":
        response = chunk.get("response")
        return _responses_output_item_annotations(response.get("output") if isinstance(response, dict) else None)
    return []


def _responses_event_search_results(chunk: Any) -> list[dict]:
    """Perplexity returns search sources separately from text annotations."""
    if hasattr(chunk, "model_dump"):
        chunk = chunk.model_dump()
    if not isinstance(chunk, dict):
        return []
    event = chunk.get("type")
    if event == "response.reasoning.search_results":
        results = chunk.get("results")
        return [item for item in results if isinstance(item, dict)] if isinstance(results, list) else []
    if event in {"response.output_item.added", "response.output_item.done"}:
        items = [chunk.get("item")]
    elif event == "response.completed":
        response = chunk.get("response")
        items = response.get("output") if isinstance(response, dict) else None
    else:
        return []
    results = []
    for item in items or []:
        if isinstance(item, dict) and item.get("type") == "search_results" and isinstance(item.get("results"), list):
            results.extend(source for source in item["results"] if isinstance(source, dict))
    return results


_BRIDGE_SUBCLASS: tuple[type, type] | None = None


def _citation_preserving_bridge() -> type:
    """Extend the installed Responses→chat bridge, which drops stream annotations.

    LiteLLM's stream translator maps only text/tool/usage events, so native
    ``url_citation`` annotations survive the non-stream path but vanish while
    streaming. Subclassing keeps reasoning, tools, usage and no-log behavior.
    """
    global _BRIDGE_SUBCLASS
    from litellm.completion_extras.litellm_responses_transformation import handler as bridge_handler
    from litellm.completion_extras.litellm_responses_transformation import transformation as bridge_transformation

    base = bridge_handler.ResponsesToCompletionBridgeHandler
    if _BRIDGE_SUBCLASS is not None and _BRIDGE_SUBCLASS[0] is base:
        return _BRIDGE_SUBCLASS[1]

    class _CitationStreamIterator(bridge_transformation.OpenAiResponsesToChatCompletionStreamIterator):
        def chunk_parser(self, chunk: Any) -> Any:
            parsed = super().chunk_parser(chunk)
            choices = getattr(parsed, "choices", None) or []
            delta = getattr(choices[0], "delta", None) if choices else None
            if delta is None:
                return parsed
            search_results = _responses_event_search_results(chunk)
            if search_results:
                # LiteLLM drops empty text chunks unless the delta carries metadata.
                fields = getattr(delta, "provider_specific_fields", None) or {}
                delta.provider_specific_fields = {**fields, "search_results": search_results}
            annotations = _responses_event_annotations(chunk)
            if annotations:
                existing = getattr(delta, "annotations", None) or []
                delta.annotations = [*existing, *annotations]
            return parsed

    class _CitationTransformation(bridge_transformation.LiteLLMResponsesTransformationHandler):
        def get_model_response_iterator(self, streaming_response, sync_stream, json_mode=False):
            return _CitationStreamIterator(streaming_response, sync_stream, json_mode)

    class _CitationBridgeHandler(base):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            super().__init__()
            self.transformation_handler = _CitationTransformation()

    _BRIDGE_SUBCLASS = (base, _CitationBridgeHandler)
    return _CitationBridgeHandler


def _strict_compatible_tool_schema(value: object) -> bool:
    """Return whether a schema satisfies strict function-mode's closed-object subset."""
    if not isinstance(value, dict):
        return False
    kind = value.get("type")
    if kind == "object":
        properties = value.get("properties")
        required = value.get("required", [])
        if (
            not isinstance(properties, dict)
            or not isinstance(required, list)
            or not all(isinstance(name, str) for name in required)
            or value.get("additionalProperties") is not False
            or set(properties) != set(required)
        ):
            return False
        return all(_strict_compatible_tool_schema(property_schema) for property_schema in properties.values())
    if kind == "array":
        return _strict_compatible_tool_schema(value.get("items"))
    return isinstance(kind, str) and kind in {"string", "number", "integer", "boolean", "null"}


def _perplexity_agent_tools(tools: list[dict] | None) -> list[dict] | None:
    """Validate OpenAI declarations before LiteLLM maps them to Responses.

    Lumen stores provider-neutral OpenAI function envelopes. The pinned LiteLLM
    Responses bridge owns their conversion to Agent's flat function-tool wire
    shape. Lumen opts into strict mode only for compatible closed schemas and
    preserves an explicit caller choice. Server dispatch always validates arguments.
    """
    if not tools:
        return None
    normalized: list[dict] = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise ValueError("perplexity agent tools must be OpenAI function declarations")
        function = tool.get("function")
        if not isinstance(function, dict):
            raise ValueError("perplexity agent function declaration is invalid")
        name = function.get("name")
        description = function.get("description")
        parameters = function.get("parameters")
        if (
            not isinstance(name, str)
            or not name
            or (description is not None and not isinstance(description, str))
            or (parameters is not None and not isinstance(parameters, dict))
        ):
            raise ValueError("perplexity agent function declaration is invalid")
        function_payload = dict(function)
        if "strict" not in function_payload and _strict_compatible_tool_schema(parameters):
            function_payload["strict"] = True
        normalized.append({"type": "function", "function": function_payload})
    return normalized


def _perplexity_optional_params(
    *,
    mode: Literal["router", "agent", "legacy"],
    model: str,
    stream: bool,
    max_tokens: int | None,
    temperature: float | None,
    tools: list[dict] | None,
    extra: dict | None,
) -> dict[str, Any]:
    blocked = {
        "api_base",
        "api_key",
        "base_url",
        "custom_llm_provider",
        "litellm_logging_obj",
        "messages",
        "model",
        "provider",
        "stream",
        "stream_options",
    }
    if mode == "agent" and model.rsplit("/", 1)[-1].lower().startswith("sonar"):
        # Sonar searches intrinsically. Do not add a second hosted search tool.
        blocked.add("web_search_options")
    optional_params = {key: value for key, value in (extra or {}).items() if key not in blocked}
    if max_tokens is not None:
        optional_params["max_tokens"] = max_tokens
    if temperature is not None:
        optional_params["temperature"] = temperature
    agent_tools = _perplexity_agent_tools(tools) if mode == "agent" else tools
    if agent_tools:
        optional_params["tools"] = agent_tools
    if mode == "agent" and "web_search_options" in optional_params:
        # LiteLLM maps options in insertion order. Place search after function
        # tools so its hosted tool is appended rather than overwritten.
        optional_params["web_search_options"] = optional_params.pop("web_search_options")
    optional_params["stream"] = stream
    return optional_params


async def _perplexity_completion(
    model: str,
    messages: list[dict],
    *,
    api_base: str | None,
    api_key: str | None,
    stream: bool,
    max_tokens: int | None,
    temperature: float | None,
    tools: list[dict] | None,
    extra: dict | None,
) -> Any:
    if not api_key:
        raise ValueError("perplexity_credentials_missing")

    import litellm

    mode = _perplexity_mode(model, api_base)
    optional_params = _perplexity_optional_params(
        mode=mode,
        model=model,
        stream=stream,
        max_tokens=max_tokens,
        temperature=temperature,
        tools=tools,
        extra=extra,
    )
    canonical_model = api_model_name(model, "perplexity")
    if mode == "router":
        params = _build_params(
            f"openai/{canonical_model}",
            messages,
            api_base=_perplexity_router_base(api_base),
            api_key=api_key,
            max_tokens=max_tokens,
            temperature=temperature,
            custom_llm_provider="openai",
            tools=tools,
            extra={
                **{key: value for key, value in optional_params.items() if key != "stream"},
                "_skip_responses_api_bridge": True,
            },
        )
        if stream:
            params["stream"] = True
            params["stream_options"] = {"include_usage": True}
        return await litellm.acompletion(**params)
    if mode == "legacy":
        params = _build_params(
            model,
            messages,
            api_base=api_base,
            api_key=api_key,
            max_tokens=max_tokens,
            temperature=temperature,
            custom_llm_provider="perplexity",
            tools=tools,
            extra={key: value for key, value in optional_params.items() if key != "stream"},
        )
        if stream:
            params["stream"] = True
            params["stream_options"] = {"include_usage": True}
        return await litellm.acompletion(**params)

    bridge_handler_cls = _citation_preserving_bridge()

    from lumen.services.providers.subscription_logging import SubscriptionLogging

    routed_model = perplexity_route_model_name(model)
    fixed_api_base = _perplexity_agent_base(api_base)
    logging_obj = SubscriptionLogging(
        model=routed_model,
        provider="perplexity",
        fixed_api_base=fixed_api_base,
        call_id=str(uuid.uuid4()),
        stream=stream,
    )
    return await bridge_handler_cls().acompletion(
        model=routed_model,
        messages=messages,
        optional_params=optional_params,
        litellm_params={
            "api_base": fixed_api_base,
            "api_key": api_key,
            "custom_llm_provider": "perplexity",
            "no_log": True,
        },
        headers={},
        model_response=litellm.ModelResponse(),
        logging_obj=logging_obj,
        custom_llm_provider="perplexity",
        stream=stream,
    )


async def acompletion(
    model: str,
    messages: list[dict],
    *,
    api_base: str | None = None,
    api_key: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    custom_llm_provider: str | None = None,
    tools: list[dict] | None = None,
    native_web_search: dict[str, Any] | None = None,
    extra: dict | None = None,
    provider_auth: ProviderAuthRef | None = None,
) -> Any:
    """비스트리밍 litellm 호출."""
    merged_extra = _native_web_search_extra(extra, native_web_search, custom_llm_provider)
    if provider_auth is not None:
        if native_web_search is not None:
            raise ProviderSubscriptionError("subscription_native_web_search_unsupported", 422)
        return await _subscription_completion(
            model,
            messages,
            provider_auth=provider_auth,
            stream=False,
            max_tokens=max_tokens,
            temperature=temperature,
            custom_llm_provider=custom_llm_provider,
            tools=tools,
            extra=merged_extra,
        )
    if _requires_subscription_auth(model, custom_llm_provider):
        raise ProviderSubscriptionError("subscription_auth_required", 502)
    effective_provider = custom_llm_provider or (
        "perplexity" if model.startswith("perplexity/") or (api_base and "perplexity" in api_base) else None
    )
    if effective_provider == "perplexity":
        return await _perplexity_completion(
            model,
            messages,
            api_base=api_base,
            api_key=api_key,
            stream=False,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            extra=merged_extra,
        )
    import litellm

    litellm.drop_params = True
    params = _build_params(
        model,
        messages,
        api_base=api_base,
        api_key=api_key,
        max_tokens=max_tokens,
        temperature=temperature,
        custom_llm_provider=custom_llm_provider,
        tools=tools,
        extra=merged_extra,
    )
    return await litellm.acompletion(**params)


async def acompletion_stream(
    model: str,
    messages: list[dict],
    *,
    api_base: str | None = None,
    api_key: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    custom_llm_provider: str | None = None,
    tools: list[dict] | None = None,
    native_web_search: dict[str, Any] | None = None,
    extra: dict | None = None,
    reasoning_effort: str | None = None,
    provider_auth: ProviderAuthRef | None = None,
) -> Any:
    """스트리밍 litellm 호출. usage 계측을 위해 include_usage 를 강제한다."""
    merged_extra = _native_web_search_extra(
        {**(extra or {}), **_reasoning_params(model, reasoning_effort, custom_llm_provider)},
        native_web_search,
        custom_llm_provider,
    )
    if provider_auth is not None:
        if native_web_search is not None:
            raise ProviderSubscriptionError("subscription_native_web_search_unsupported", 422)
        return await _subscription_completion(
            model,
            messages,
            provider_auth=provider_auth,
            stream=True,
            max_tokens=max_tokens,
            temperature=temperature,
            custom_llm_provider=custom_llm_provider,
            tools=tools,
            extra=merged_extra,
        )
    if _requires_subscription_auth(model, custom_llm_provider):
        raise ProviderSubscriptionError("subscription_auth_required", 502)
    effective_provider = custom_llm_provider or (
        "perplexity" if model.startswith("perplexity/") or (api_base and "perplexity" in api_base) else None
    )
    if effective_provider == "perplexity":
        return await _perplexity_completion(
            model,
            messages,
            api_base=api_base,
            api_key=api_key,
            stream=True,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            extra=merged_extra or None,
        )
    import litellm

    litellm.drop_params = True
    params = _build_params(
        model,
        messages,
        api_base=api_base,
        api_key=api_key,
        max_tokens=max_tokens,
        temperature=temperature,
        custom_llm_provider=custom_llm_provider,
        tools=tools,
        extra=merged_extra or None,
    )
    params["stream"] = True
    params["stream_options"] = {"include_usage": True}
    return await litellm.acompletion(**params)


def _native_protocol_params(
    *,
    api_base: str | None,
    api_key: str | None,
    custom_llm_provider: str | None,
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if api_base:
        params["api_base"] = api_base
    if api_key:
        params["api_key"] = api_key
    if custom_llm_provider:
        params["custom_llm_provider"] = custom_llm_provider
    return params


async def aresponses(
    *,
    model: str,
    input: str | list[dict],
    stream: bool,
    api_base: str | None,
    api_key: str | None,
    custom_llm_provider: str | None,
    provider_auth: ProviderAuthRef | None,
    include: list[str] | None = None,
    instructions: str | None = None,
    max_output_tokens: int | None = None,
    metadata: dict | None = None,
    parallel_tool_calls: bool | None = None,
    prompt_cache_key: str | None = None,
    reasoning: dict | None = None,
    temperature: float | None = None,
    text: dict | None = None,
    tool_choice: Any = None,
    tools: list[dict] | None = None,
    top_p: float | None = None,
    truncation: str | None = None,
    user: str | None = None,
    service_tier: str | None = None,
    safety_identifier: str | None = None,
    context_management: list[dict] | None = None,
) -> Any:
    """Call LiteLLM's native Responses transport without forwarding arbitrary request keys."""
    if provider_auth is not None or _requires_subscription_auth(model, custom_llm_provider):
        raise ProviderSubscriptionError("subscription_protocol_unsupported", 400)
    import litellm

    params: dict[str, Any] = {
        "model": model,
        "input": input,
        "stream": stream,
        **_native_protocol_params(
            api_base=api_base,
            api_key=api_key,
            custom_llm_provider=custom_llm_provider,
        ),
    }
    optional = {
        "include": include,
        "instructions": instructions,
        "max_output_tokens": max_output_tokens,
        "metadata": metadata,
        "parallel_tool_calls": parallel_tool_calls,
        "prompt_cache_key": prompt_cache_key,
        "reasoning": reasoning,
        "temperature": temperature,
        "text": text,
        "tool_choice": tool_choice,
        "tools": tools,
        "top_p": top_p,
        "truncation": truncation,
        "user": user,
        "service_tier": service_tier,
        "safety_identifier": safety_identifier,
        "context_management": context_management,
    }
    params.update({key: value for key, value in optional.items() if value is not None})
    return await litellm.aresponses(**params)


async def aanthropic_messages(
    *,
    model: str,
    messages: list[dict],
    max_tokens: int,
    stream: bool,
    api_base: str | None,
    api_key: str | None,
    custom_llm_provider: str | None,
    provider_auth: ProviderAuthRef | None,
    system: Any = None,
    metadata: dict | None = None,
    stop_sequences: list[str] | None = None,
    temperature: float | None = None,
    thinking: dict | None = None,
    context_management: dict | None = None,
    tool_choice: dict | None = None,
    tools: list[dict] | None = None,
    top_k: int | None = None,
    top_p: float | None = None,
    container: dict | None = None,
    output_config: dict | None = None,
    anthropic_headers: dict[str, str] | None = None,
) -> Any:
    """Call LiteLLM's native Anthropic transport, including Anthropic subscription auth."""
    import litellm

    fingerprint: str | None = None
    if provider_auth is not None:
        from lumen.services.providers import subscriptions

        if provider_auth.get("auth_mode") != "anthropic_subscription" or custom_llm_provider != "anthropic":
            raise ProviderSubscriptionError("subscription_protocol_unsupported", 400)
        credential = await subscriptions.resolve_subscription_credential(provider_auth)
        access_token = credential.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ProviderSubscriptionError("subscription_auth_required", 502)
        api_key = access_token
        api_base = "https://api.anthropic.com"
        fingerprint = credential.get("_fingerprint") if isinstance(credential.get("_fingerprint"), str) else None
    elif _requires_subscription_auth(model, custom_llm_provider):
        raise ProviderSubscriptionError("subscription_auth_required", 502)

    params: dict[str, Any] = {
        "model": litellm_model_name(model),
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": stream,
        **_native_protocol_params(
            api_base=api_base,
            api_key=api_key,
            custom_llm_provider=custom_llm_provider,
        ),
    }
    optional = {
        "system": system,
        "metadata": metadata,
        "stop_sequences": stop_sequences,
        "temperature": temperature,
        "thinking": thinking,
        "context_management": context_management,
        "tool_choice": tool_choice,
        "tools": tools,
        "top_k": top_k,
        "top_p": top_p,
        "container": container,
        "output_config": output_config,
        "extra_headers": anthropic_headers,
    }
    params.update({key: value for key, value in optional.items() if value is not None})
    try:
        result = await litellm.anthropic.messages.acreate(**params)
        if stream:
            source = (
                _guard_subscription_stream(result, provider_auth, fingerprint) if provider_auth is not None else result
            )
            return _anthropic_stream_events(source)
        return result
    except ProviderSubscriptionError:
        raise
    except BaseException as error:
        if provider_auth is None:
            raise
        safe_error = _subscription_error(error)
        if safe_error.code == "subscription_auth_required" and fingerprint is not None:
            from lumen.services.providers import subscriptions

            await subscriptions._mark_subscription_credential_rejected(provider_auth, fingerprint)
        raise safe_error from None
