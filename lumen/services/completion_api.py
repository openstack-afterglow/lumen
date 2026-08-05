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
from decimal import Decimal
from typing import Any

from lumen.config import get_settings
from lumen.services import credit, litellm_client
from lumen.services import provider_store as ps

logger = logging.getLogger(__name__)

_MAX_TOKENS_CAP = 4096


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
    except ps.ChatStorageUnavailable as exc:
        raise CompletionError(503, "일시적으로 사용할 수 없습니다") from exc
    if resolved is None:
        raise CompletionError(404, f"모델을 찾을 수 없습니다: {model}")
    return resolved


async def precheck(user_id: str, project_id: str) -> None:
    """쿼터 fail-closed. 초과 429, 저장소 장애 503."""
    try:
        await credit.precheck(user_id, project_id)
    except credit.QuotaExceeded as exc:
        raise CompletionError(429, "월 사용 한도를 초과했습니다") from exc
    except credit.ChatStorageUnavailable as exc:
        raise CompletionError(503, "일시적으로 사용할 수 없습니다") from exc


def clamp_max_tokens(requested: int | None) -> int:
    return min(requested or _MAX_TOKENS_CAP, _MAX_TOKENS_CAP)


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
    pt, ct = litellm_client.extract_usage(model_name, messages, text, final_usage)
    usage_cost = litellm_client.cost_from_usage(
        model_name,
        pt,
        ct,
        input_price_per_token=resolved.get("input_price_per_token"),
        output_price_per_token=resolved.get("output_price_per_token"),
        price_source=resolved.get("price_source"),
        provider_type=resolved.get("provider_type"),
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


def _reasoning_effort(explicit: str | None) -> str | None:
    return explicit or get_settings().chat_reasoning_effort


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
) -> dict:
    """비스트리밍 완료 — 전체 응답 반환 + 과금. tool_calls 는 릴레이(서버 미실행)."""
    resp = await litellm_client.acompletion(
        resolved["model_name"],
        messages,
        api_base=resolved.get("api_base"),
        api_key=resolved.get("api_key"),
        custom_llm_provider=resolved.get("provider_type"),
        max_tokens=clamp_max_tokens(max_tokens),
        temperature=temperature,
        tools=tools,
    )
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
        "model": resolved["model_name"],
        "content": content,
        "tool_calls": tool_calls,
        "finish_reason": finish_reason,
        "prompt_tokens": pt,
        "completion_tokens": ct,
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
            reasoning_effort=_reasoning_effort(None),
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
        }
    finally:
        if not charged and text:
            try:
                await _bill(
                    resolved, messages, text, final_usage, user_id=user_id, project_id=project_id, api_key_id=api_key_id
                )
            except Exception:
                logger.warning("API 스트림 종료 후 과금 실패", exc_info=True)
