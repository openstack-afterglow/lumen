"""OpenAI/Anthropic 호환 외부 API 공통 완료 코어 (stateless).

웹 경로(`api/chat/completions.py` — 대화 트리 저장·자체 SSE 계약)와 **독립**이다. 서비스 계층만
재사용한다: `provider_store.resolve_model` → `credit.precheck` → `litellm_client` 직접
스트리밍/비스트리밍(요청 `tools` pass-through) → `credit.apply_usage(source="api", api_key_id=…)`.
대화 트리에 저장하지 않는다(stateless). 도구는 서버가 실행하지 않고 `tool_calls` 를 릴레이한다
(표준 OpenAI/Anthropic function-calling — 호출자가 실행 후 재요청).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from decimal import Decimal
from typing import Any, Literal

from lumen.config import get_settings
from lumen.services import context_manager, credit, litellm_client, native_compaction
from lumen.services.capabilities import reasoning_can_be_disabled
from lumen.services.providers import errors
from lumen.services.providers import routing as ps
from lumen.services.usage_breakdown import UsageBreakdown

logger = logging.getLogger(__name__)

_DEFAULT_MAX_TOKENS = 4096
_ROUTING_PROVIDER_PREFIXES = frozenset(
    {
        "anthropic",
        "azure",
        "bedrock",
        "deepseek",
        "gemini",
        "ollama",
        "openai",
        "openrouter",
        "perplexity",
        "vertex_ai",
    }
)


def select_api_provider(model: str, body_provider: str | None, header_provider: str | None) -> str | None:
    if body_provider and header_provider and body_provider != header_provider:
        raise CompletionError(400, "provider_header_conflict")
    selected = body_provider or header_provider
    prefix, separator, _bare = model.partition("/")
    if (
        body_provider is None
        and header_provider
        and separator
        and prefix in _ROUTING_PROVIDER_PREFIXES
        and prefix != header_provider
    ):
        raise CompletionError(400, "provider_header_conflict")
    return selected


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
    breakdown = litellm_client.extract_usage_breakdown(model_name, messages, text, final_usage)
    pt, ct = breakdown.input_tokens, breakdown.output_tokens
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
    )
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
    extra_kwargs: dict[str, Any] = {}
    if tool_choice is not None:
        extra_kwargs["tool_choice"] = tool_choice
    try:
        resp = await litellm_client.acompletion(
            resolved["model_name"],
            messages,
            api_base=resolved.get("api_base"),
            api_key=resolved.get("api_key"),
            custom_llm_provider=resolved.get("provider_type"),
            max_tokens=clamp_max_tokens(max_tokens),
            temperature=temperature,
            tools=tools,
            extra=extra_kwargs or None,
            provider_auth=resolved.get("provider_auth"),
        )
    except errors.ProviderSubscriptionError as exc:
        raise CompletionError(exc.status_code, exc.message) from None
    choice = resp.choices[0]
    msg = choice.message
    content = getattr(msg, "content", None) or ""
    tool_calls = _norm_tool_calls(getattr(msg, "tool_calls", None))
    finish_reason = getattr(choice, "finish_reason", None) or "stop"
    pt, ct, credited = await _bill(
        resolved,
        messages,
        content,
        getattr(resp, "usage", None),
        event_id=str(uuid.uuid4()),
        user_id=user_id,
        project_id=project_id,
        api_key_id=api_key_id,
    )
    return {
        "model": resolved["api_model_name"],
        "content": content,
        "tool_calls": tool_calls,
        "finish_reason": finish_reason,
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "cache_read_input_tokens": (
            UsageBreakdown.from_runtime(getattr(resp, "usage", None)) or UsageBreakdown(0, 0)
        ).cache_read_input_tokens,
        "credited_cost": float(credited),
    }


async def complete_stream(
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
    event_id = str(uuid.uuid4())  # 요청당 1개 — 정상/finally 재과금 모두 재사용(멱등, 이중과금 방지)
    extra_kwargs: dict[str, Any] = {}
    if tool_choice is not None:
        extra_kwargs["tool_choice"] = tool_choice
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

    text = "".join(text_parts)
    try:
        pt, ct, credited = await _bill(
            resolved,
            messages,
            text,
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
        if not charged and text:
            try:
                await _bill(
                    resolved,
                    messages,
                    text,
                    final_usage,
                    event_id=event_id,
                    user_id=user_id,
                    project_id=project_id,
                    api_key_id=api_key_id,
                )
            except Exception:
                logger.warning("API 스트림 종료 후 과금 실패", exc_info=True)


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


def _native_usage(value: dict, *, protocol: Literal["anthropic", "responses"]) -> dict | None:
    """Map raw provider-native usage to the runtime dict ``_bill`` consumes.

    Anthropic ``input_tokens`` excludes cache, so cache read/creation are added
    to reach total input; Responses ``input_tokens`` already includes cached
    tokens, which must not be added again. Under provider-native compaction the
    top-level counters cover the non-compaction iterations only, so the
    per-iteration breakdown is authoritative whenever the provider sends one.
    """
    usage = value.get("usage")
    if not isinstance(usage, dict):
        return None
    breakdown = (
        UsageBreakdown.from_anthropic(usage) if protocol == "anthropic" else UsageBreakdown.from_responses(usage)
    )
    return breakdown.as_usage_dict() if breakdown is not None else None


def _responses_input_messages(input_value: str | list[dict]) -> list[dict]:
    if isinstance(input_value, str):
        return [{"role": "user", "content": input_value}]
    return [item for item in input_value if isinstance(item, dict)]


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
    event_id = str(uuid.uuid4())
    options = _with_passthrough_compaction(options, resolved=resolved, protocol="responses")
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
    except errors.ProviderSubscriptionError as exc:
        raise CompletionError(exc.status_code, exc.message) from None
    except Exception as exc:
        logger.warning("Responses upstream request failed model=%s", resolved.get("model_name"), exc_info=True)
        raise CompletionError(502, "upstream model error") from exc

    messages = _responses_input_messages(input)
    if not stream:
        payload = _native_dict(response)
        await _bill(
            resolved,
            messages,
            _native_text(payload.get("output", [])),
            _native_usage(payload, protocol="responses"),
            event_id=event_id,
            user_id=user_id,
            project_id=project_id,
            api_key_id=api_key_id,
        )
        return payload

    async def events() -> AsyncIterator[dict]:
        charged = False
        text_parts: list[str] = []
        try:
            async for raw_event in response:
                event = _native_dict(raw_event)
                event_type = event.get("type")
                if event_type == "response.output_text.delta" and isinstance(event.get("delta"), str):
                    text_parts.append(event["delta"])
                if event_type == "response.completed" and isinstance(event.get("response"), dict):
                    completed = event["response"]
                    await _bill(
                        resolved,
                        messages,
                        "".join(text_parts) or _native_text(completed.get("output", [])),
                        _native_usage(completed, protocol="responses"),
                        event_id=event_id,
                        user_id=user_id,
                        project_id=project_id,
                        api_key_id=api_key_id,
                    )
                    charged = True
                yield event
        finally:
            if not charged and text_parts:
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

    return events()


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
            _native_usage(payload, protocol="anthropic"),
            event_id=event_id,
            user_id=user_id,
            project_id=project_id,
            api_key_id=api_key_id,
        )
        return payload

    async def events() -> AsyncIterator[dict]:
        start_usage: dict = {}
        delta_usages: list[dict] = []
        text_parts: list[str] = []
        charged = False
        try:
            async for raw_event in response:
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
                    breakdown = UsageBreakdown.from_anthropic_stream(start_usage, delta_usages)
                    await _bill(
                        resolved,
                        messages,
                        "".join(text_parts),
                        breakdown.as_usage_dict() if breakdown is not None else None,
                        event_id=event_id,
                        user_id=user_id,
                        project_id=project_id,
                        api_key_id=api_key_id,
                    )
                    charged = True
                yield event
        finally:
            if not charged and text_parts:
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

    return events()


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
