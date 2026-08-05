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

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

logger = logging.getLogger(__name__)

_RAW_COST_QUANTUM = Decimal("0.0000000001")
_TOKENS_PER_MILLION = Decimal("1000000")


@dataclass(frozen=True)
class UsageCost:
    raw_cost: Decimal
    input_cost: Decimal
    output_cost: Decimal
    pricing_status: Literal["priced", "partial", "unpriced"]
    pricing_snapshot: dict[str, object]


def count_tokens(model: str, *, messages: list[dict] | None = None, text: str | None = None) -> int:
    """litellm.token_counter 로 토큰 수 산출. 실패 시 대략치(4 chars ≈ 1 token) 폴백."""
    try:
        import litellm

        if messages is not None:
            return int(litellm.token_counter(model=model, messages=messages))
        return int(litellm.token_counter(model=model, text=text or ""))
    except Exception:
        logger.warning("litellm token_counter 실패 model=%s — 대략치 폴백", model, exc_info=True)
        raw = text if text is not None else "".join(str(m.get("content", "")) for m in (messages or []))
        return max(1, len(raw) // 4)


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


def _litellm_component_rates(
    model: str, prompt_tokens: int, completion_tokens: int, provider_type: str | None = None
) -> tuple[Decimal | None, Decimal | None]:
    lookup_prompt_tokens = max(1, prompt_tokens)
    lookup_completion_tokens = max(1, completion_tokens)
    try:
        import litellm

        kwargs = {
            "model": model,
            "prompt_tokens": lookup_prompt_tokens,
            "completion_tokens": lookup_completion_tokens,
        }
        if provider_type:
            kwargs["custom_llm_provider"] = provider_type
        prompt_cost, completion_cost = litellm.cost_per_token(**kwargs)
    except Exception:
        logger.warning("litellm cost_per_token 실패 model=%s", model, exc_info=True)
        return None, None
    prompt_total = _as_decimal(prompt_cost)
    completion_total = _as_decimal(completion_cost)
    prompt_rate = prompt_total / lookup_prompt_tokens if prompt_total is not None else None
    completion_rate = completion_total / lookup_completion_tokens if completion_total is not None else None
    return prompt_rate, completion_rate


def _component_cost(
    *,
    tokens: int,
    stored_rate: Decimal | None,
    stored_source: str | None,
    fallback_rate: Decimal | None,
) -> tuple[Decimal, Decimal | None, str | None, bool]:
    if stored_rate is not None:
        return stored_rate * tokens, stored_rate, stored_source, True
    if fallback_rate is not None:
        return fallback_rate * tokens, fallback_rate, "litellm", True
    return Decimal("0"), None, None, False


def effective_prices_per_million(model: str, provider_type: str | None = None) -> tuple[Decimal | None, Decimal | None]:
    """Return LiteLLM's bundled base input/output prices for display only."""
    input_rate, output_rate = _litellm_component_rates(model, 1_000_000, 1_000_000, provider_type)

    def display_price(rate: Decimal | None) -> Decimal | None:
        return (
            (rate * _TOKENS_PER_MILLION).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
            if rate is not None
            else None
        )

    return display_price(input_rate), display_price(output_rate)


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
    fallback_input, fallback_output = (
        _litellm_component_rates(model, prompt_tokens, completion_tokens, provider_type)
        if input_price_per_token is None or output_price_per_token is None
        else (None, None)
    )
    input_cost, input_rate, input_source, input_priced = _component_cost(
        tokens=prompt_tokens,
        stored_rate=input_price_per_token,
        stored_source=price_source,
        fallback_rate=fallback_input,
    )
    output_cost, output_rate, output_source, output_priced = _component_cost(
        tokens=completion_tokens,
        stored_rate=output_price_per_token,
        stored_source=price_source,
        fallback_rate=fallback_output,
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
    """지원 모델에만 reasoning_effort 를 붙인다(추론 노출). 미지원/비활성/오류 시 {}.

    litellm 이 provider 별 파라미터(Anthropic thinking budget, Gemini thinking, o-series 등)로
    정규화한다. supports_reasoning 이 false 면 요청에 넣지 않아 비용/동작 변화가 없다.
    """
    if effort is None:
        return {}
    normalized = effort.strip().lower()
    if normalized in _OMIT_EFFORTS:
        return {}
    if normalized != "none" and normalized not in _VALID_EFFORTS:
        return {}
    try:
        import litellm

        supported = litellm.supports_reasoning(model=model)
    except Exception:
        logger.warning("litellm supports_reasoning 조회 실패 model=%s", model, exc_info=True)
        return {}
    if not supported:
        return {}
    return {"reasoning_effort": normalized}


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
    extra: dict | None = None,
) -> Any:
    """비스트리밍 litellm 호출."""
    import litellm

    # 모델별 미지원 파라미터(tools/temperature 등)를 자동 드롭 — 예: gpt-5-search-api 는 tools 미지원.
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
        extra=extra,
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
    extra: dict | None = None,
    reasoning_effort: str | None = None,
) -> Any:
    """스트리밍 litellm 호출. usage 계측을 위해 include_usage 를 강제한다.

    반환값은 async iterator(청크). 호출부가 청크 델타를 SSE 로 전달하고,
    마지막 usage 청크를 extract_usage 에 넘겨 과금한다. tools 지정 시 tool_call 델타도 스트리밍된다.
    reasoning_effort 는 지원 모델에만 붙어 추론(reasoning_content)을 스트리밍시킨다(_reasoning_params).
    """
    import litellm

    # 모델별 미지원 파라미터(tools/temperature 등)를 자동 드롭 — 예: gpt-5-search-api 는 tools 미지원.
    litellm.drop_params = True

    merged_extra = {**(extra or {}), **_reasoning_params(model, reasoning_effort, custom_llm_provider)}
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
