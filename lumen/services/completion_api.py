"""OpenAI/Anthropic 호환 외부 API 공통 완료 코어 (stateless).

웹 경로(`api/chat/completions.py` — 대화 트리 저장·자체 SSE 계약)와 **독립**이다. 서비스 계층만
재사용한다: `provider_store.resolve_model` → `credit.precheck` → `litellm_client` 직접
스트리밍/비스트리밍(요청 `tools` pass-through) → `credit.apply_usage(source="api", api_key_id=…)`.
대화 트리에 저장하지 않는다(stateless). 도구는 서버가 실행하지 않고 `tool_calls` 를 릴레이한다
(표준 OpenAI/Anthropic function-calling — 호출자가 실행 후 재요청).

Provider invocation/normalization (`invoke_chat_once`, `invoke_responses_once`) is separate
from billing: online wrappers bill with a request-scoped UUID event, while durable Batch
runs (`durable_runs.api_completion`) settle the same normalized result against their
frozen run ledger with event ``run:<run_id>``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Literal, NoReturn

import anyio
from litellm.exceptions import BadRequestError

from lumen.config import get_settings
from lumen.services import context_manager, credit, litellm_client, native_compaction
from lumen.services.capabilities import reasoning_can_be_disabled
from lumen.services.providers import errors
from lumen.services.providers import routing as ps
from lumen.services.providers.pricing import frozen_token_pricing
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

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(message)


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


async def resolve_api(model: str, *, provider: str | None = None) -> dict:
    """Resolve one public compatibility API route, rejecting ambiguous catalogs."""
    if not model:
        raise CompletionError(400, "model 이 필요합니다")
    try:
        resolved = await ps.resolve_api_model(model, provider=provider)
    except errors.AmbiguousModelRouteError as exc:
        raise CompletionError(409, "model_route_ambiguous") from exc
    except errors.ChatStorageUnavailable as exc:
        raise CompletionError(503, "일시적으로 사용할 수 없습니다") from exc
    if resolved is None:
        raise CompletionError(404, f"모델을 찾을 수 없습니다: {model}")
    return resolved


async def precheck(user_id: str, project_id: str, api_key_id: int | None = None) -> None:
    """쿼터 fail-closed. 초과 429, 저장소 장애 503."""
    try:
        await credit.precheck(user_id, project_id, api_key_id=api_key_id)
    except credit.QuotaExceeded as exc:
        raise CompletionError(429, str(exc)) from exc
    except credit.ChatStorageUnavailable as exc:
        raise CompletionError(503, "일시적으로 사용할 수 없습니다") from exc


def clamp_max_tokens(requested: int | None) -> int:
    """Apply only the legacy default; explicit positive budgets are never capped."""
    value = _DEFAULT_MAX_TOKENS if requested is None else requested
    if value <= 0:
        raise CompletionError(422, "max token budget must be positive")
    return value


def billing_route(
    resolved: dict, messages: list[dict], *, protocol: str, options: dict | None = None
) -> dict:
    """Freeze this stateless request's route/prices and reject unmeterable priced media.

    The chat allowlist is evidence about its transport, not a model-name guess.
    Native protocol adapters do not establish equivalent modality metering.
    Online compatibility APIs hold no durable reservation; durable Batch
    preparation reuses this freeze and adds its own frozen bound and hold.
    """

    route = deepcopy(resolved)
    try:
        route["token_rates"] = frozen_token_pricing(route)["token_rates"]
    except errors.ProviderValidationError as exc:
        raise CompletionError(422, "model token pricing is invalid") from exc
    output_modalities = list((options or {}).get("modalities") or [])
    if any(tool.get("type") == "image_generation" for tool in (options or {}).get("tools") or []):
        output_modalities.append("image")
    required = required_modalities_for_request(
        route["token_rates"], messages=messages, output_modalities=output_modalities
    )
    route["required_token_modalities"] = required
    reporting = (
        MODALITY_INPUT_REPORTING_PROVIDERS.get(str(route.get("provider_type") or ""), frozenset())
        if protocol == "chat" and route.get("provider_auth") is None else frozenset()
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
        breakdown = litellm_client.extract_usage_breakdown(model_name, messages, text, final_usage)
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
) -> tuple[int, int, Decimal]:
    """extract_usage → cost_from_usage → apply_usage(source="api"). (pt, ct, credited) 반환.

    event_id 는 요청당 1개를 재사용한다(정상/finally 재과금 모두 동일 id) — apply_usage 의
    event_id unique + IntegrityError 흡수로 정확히 1회 과금(중복 방지). 새 uuid 마다 생성하면
    이 멱등성이 깨져 이중 과금·통계 이중집계가 발생한다.
    """
    model_name = resolved["model_name"]
    token_rates = resolved.get("token_rates")
    if token_rates is None:
        token_rates = frozen_token_pricing(resolved)["token_rates"]
    required = resolved.get("required_token_modalities")
    if required is None:
        required = required_modalities_for_request(token_rates, messages=messages)
    breakdown = usage_breakdown_for_billing(
        model_name, token_rates=token_rates, required=required, messages=messages, text=text,
        final_usage=final_usage,
    )
    pt, ct = breakdown.input_tokens, breakdown.output_tokens
    try:
        usage_cost = litellm_client.cost_from_usage(
            model_name,
            pt,
            ct,
            input_price_per_token=resolved.get("input_price_per_token"),
            output_price_per_token=resolved.get("output_price_per_token"),
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
            token_rates=token_rates,
            required_modalities=required,
        )
    except ValueError as exc:
        if token_rates or required:
            raise CompletionError(502, "modality_usage_unavailable") from exc
        raise
    credited = await credit.apply_usage(
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
        breakdown=breakdown,
    )
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
    max_tokens: int,
    temperature: float | None,
    tools: list[dict] | None = None,
    tool_choice: Any = None,
    provider_once: bool = False,
) -> ChatInvocation:
    """Send one nonstreaming chat request on an already frozen route; no billing.

    Provider exceptions propagate unchanged so each caller keeps its own error
    contract. ``provider_once`` disables every transport retry/re-send layer.
    """
    extra_kwargs: dict[str, Any] = {}
    if tool_choice is not None:
        extra_kwargs["tool_choice"] = tool_choice
    resp = await litellm_client.acompletion(
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
    choice = resp.choices[0]
    msg = choice.message
    return ChatInvocation(
        model=resolved["api_model_name"],
        content=getattr(msg, "content", None) or "",
        tool_calls=_norm_tool_calls(getattr(msg, "tool_calls", None)),
        finish_reason=getattr(choice, "finish_reason", None) or "stop",
        usage=getattr(resp, "usage", None),
    )


async def complete_once(
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
) -> dict:
    """비스트리밍 완료 — 전체 응답 반환 + 과금. tool_calls 는 릴레이(서버 미실행)."""
    resolved = billing_route(resolved, messages, protocol="chat", options={"tools": tools})
    try:
        invocation = await invoke_chat_once(
            resolved=resolved,
            messages=messages,
            max_tokens=clamp_max_tokens(max_tokens),
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
        )
    except errors.ProviderSubscriptionError as exc:
        raise CompletionError(exc.status_code, exc.message) from None
    pt, ct, credited = await _bill(
        resolved,
        messages,
        invocation.content,
        invocation.usage,
        event_id=str(uuid.uuid4()),
        user_id=user_id,
        project_id=project_id,
        api_key_id=api_key_id,
    )
    return {
        "model": invocation.model,
        "content": invocation.content,
        "tool_calls": invocation.tool_calls,
        "finish_reason": invocation.finish_reason,
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "cache_read_input_tokens": (
            UsageBreakdown.from_runtime(invocation.usage) or UsageBreakdown(0, 0)
        ).cache_read_input_tokens,
        "credited_cost": float(credited),
    }


def complete_stream(
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
) -> AsyncIterator[dict]:
    """Validate/freeze before HTTP streaming starts, then return the completion iterator."""
    resolved = billing_route(resolved, messages, protocol="chat", options={"tools": tools})
    return _complete_stream(
        resolved=resolved, messages=messages, user_id=user_id, project_id=project_id,
        api_key_id=api_key_id, max_tokens=max_tokens, temperature=temperature,
        tools=tools, tool_choice=tool_choice,
    )


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
    event_id = str(uuid.uuid4())  # 요청당 1개 — 정상/finally 재과금 모두 재사용(멱등, 이중과금 방지)
    extra_kwargs: dict[str, Any] = {}
    if tool_choice is not None:
        extra_kwargs["tool_choice"] = tool_choice
    # One finally owns the provider stream and partial billing: a consumer close
    # (GeneratorExit) at any delta yield must still close upstream and bill.
    try:
        try:
            gen = await litellm_client.acompletion_stream(
                resolved["model_name"],
                messages,
                api_base=resolved.get("api_base"),
                api_key=resolved.get("api_key"),
                custom_llm_provider=resolved.get("provider_type"),
                max_tokens=clamp_max_tokens(max_tokens),
                temperature=temperature,
                tools=tools,
                extra=extra_kwargs or None,
                reasoning_effort=_reasoning_effort(None, resolved),
                provider_auth=resolved.get("provider_auth"),
            )
            async for chunk in gen:
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
            logger.warning("API 구독 스트리밍 실패 model=%s code=%s", resolved.get("model_name"), exc.code)
            yield {"type": "error", "code": exc.code, "message": exc.message}
            return
        except Exception:
            logger.warning("API 스트리밍 실패 model=%s", resolved.get("model_name"), exc_info=True)
            yield {"type": "error", "message": "생성 중 오류가 발생했습니다"}
            return

        pt, ct, credited = await _bill(
            resolved,
            messages,
            "".join(text_parts),
            final_usage,
            event_id=event_id,
            user_id=user_id,
            project_id=project_id,
            api_key_id=api_key_id,
        )
        charged = True
        yield {
            "type": "done",
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "finish_reason": finish_reason,
            "credited_cost": float(credited),
            "cache_read_input_tokens": (
                UsageBreakdown.from_runtime(final_usage) or UsageBreakdown(0, 0)
            ).cache_read_input_tokens,
        }
    finally:
        # An ASGI disconnect is a level-triggered AnyIO cancel that can land at the
        # upstream read; unshielded cleanup awaits would be re-cancelled and skip billing.
        with anyio.CancelScope(shield=True):
            try:
                await _close_provider_stream(gen)
            finally:
                if not charged and text_parts and not resolved.get("token_rates"):
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


class _ProviderEvents:
    """Native events that release their eagerly opened provider stream even if never iterated.

    Routes register this with the ASGI SSE owner, whose cleanup runs even when the
    response body generator never starts. ``aclose`` is idempotent.
    """

    def __init__(self, events: AsyncIterator[dict], provider_stream: Any):
        self._events = events
        self._provider_stream = provider_stream
        self._closed = False

    def __aiter__(self) -> _ProviderEvents:
        return self

    async def __anext__(self) -> dict:
        return await self._events.__anext__()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._events.aclose()  # a started stream settles billing here
        finally:
            await _close_provider_stream(self._provider_stream)


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
    options = dict(options)
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
    response = await litellm_client.aresponses(
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
    return _native_dict(response)


def _raise_responses_error(exc: Exception, resolved: dict) -> NoReturn:
    """Map one Responses provider failure to the online compatibility error contract."""
    if isinstance(exc, CompletionError):
        raise exc
    if isinstance(exc, errors.ProviderSubscriptionError):
        raise CompletionError(exc.status_code, exc.message) from None
    if isinstance(exc, BadRequestError):
        logger.info("Responses provider rejected request model=%s", resolved.get("model_name"))
        raise CompletionError(400, "upstream model rejected request") from None
    logger.warning("Responses upstream request failed model=%s", resolved.get("model_name"), exc_info=exc)
    raise CompletionError(502, "upstream model error") from exc


async def complete_responses(
    *,
    resolved: dict,
    input: str | list[dict],
    stream: bool,
    user_id: str,
    project_id: str,
    api_key_id: int | None,
    options: dict[str, Any],
) -> dict | AsyncIterator[dict]:
    """Execute the native Responses protocol and preserve every upstream item/event."""
    messages = responses_input_messages(input)
    resolved = billing_route(resolved, messages, protocol="responses", options=options)
    event_id = str(uuid.uuid4())
    options = responses_provider_options(
        _with_passthrough_compaction(options, resolved=resolved, protocol="responses"), resolved=resolved
    )
    if not stream:
        try:
            payload = await invoke_responses_once(resolved=resolved, input=input, options=options)
        except Exception as exc:
            _raise_responses_error(exc, resolved)
        text, usage = responses_usage(payload, strict=bool(resolved.get("token_rates")))
        await _bill(
            resolved,
            messages,
            text,
            usage,
            event_id=event_id,
            user_id=user_id,
            project_id=project_id,
            api_key_id=api_key_id,
        )
        return payload
    try:
        response = await litellm_client.aresponses(
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
        _raise_responses_error(exc, resolved)

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
                    completed = event["response"]
                    settlement_attempted = True
                    await _bill(
                        resolved,
                        messages,
                        "".join(text_parts) or _native_text(completed.get("output", [])),
                        _native_usage(completed, protocol="responses", strict=bool(resolved.get("token_rates"))),
                        event_id=event_id,
                        user_id=user_id,
                        project_id=project_id,
                        api_key_id=api_key_id,
                    )
                    charged = True
                yield event
        finally:
            # Stop upstream generation first; close failures are logged, never skip billing.
            await _close_provider_stream(response)
            if (
                not charged and text_parts and not resolved.get("required_token_modalities")
                and not (resolved.get("token_rates") and settlement_attempted)
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

    return _ProviderEvents(events(), response)


async def complete_anthropic(
    *,
    resolved: dict,
    messages: list[dict],
    max_tokens: int,
    stream: bool,
    user_id: str,
    project_id: str,
    api_key_id: int | None,
    options: dict[str, Any],
) -> dict | AsyncIterator[dict]:
    """Execute the native Anthropic protocol without lossy OpenAI conversion."""
    resolved = billing_route(resolved, messages, protocol="anthropic", options=options)
    event_id = str(uuid.uuid4())
    options = _with_passthrough_compaction(options, resolved=resolved, protocol="anthropic")
    try:
        response = await litellm_client.aanthropic_messages(
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
        raise CompletionError(exc.status_code, exc.message) from None
    except Exception as exc:
        logger.warning("Anthropic upstream request failed model=%s", resolved.get("model_name"), exc_info=True)
        raise CompletionError(502, "upstream model error") from exc

    if not stream:
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
        )
        return payload

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
                    )
                    charged = True
                yield event
        finally:
            # Stop upstream generation first; close failures are logged, never skip billing.
            await _close_provider_stream(response)
            if (
                not charged and text_parts and not resolved.get("required_token_modalities")
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
                        litellm_client.count_tokens(resolved["model_name"], text=completion_text),
                    )
                    if reported is not None and reported.input_tokens > 0:
                        partial = replace(reported, output_tokens=output_tokens)
                    else:
                        partial = UsageBreakdown.from_totals(
                            litellm_client.count_tokens(resolved["model_name"], messages=messages),
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

    return _ProviderEvents(events(), upstream)


async def count_anthropic_tokens(
    *,
    resolved: dict,
    payload: dict[str, Any],
    anthropic_headers: dict[str, str] | None = None,
) -> dict[str, int]:
    """Use Anthropic's authoritative count endpoint or fail explicitly when unavailable."""
    if resolved.get("provider_type") != "anthropic" or resolved.get("provider_auth") is not None:
        raise CompletionError(501, "token_count_unavailable")
    api_key = resolved.get("api_key")
    if not isinstance(api_key, str) or not api_key:
        raise CompletionError(501, "token_count_unavailable")
    base = str(resolved.get("api_base") or "https://api.anthropic.com").rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    body = dict(payload)
    body["model"] = litellm_client.litellm_model_name(resolved["model_name"])
    try:
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
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
