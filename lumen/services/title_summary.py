"""대화 제목 자동 요약 — 관리자 지정 요약 모델(is_title_model)로 첫 교환을 짧은 제목으로 요약.

⚠️ 요약 호출 비용은 사용자 크레딧이 아니라 **시스템 부담**이다:
usage_logs 에 source="system" 으로 원장만 남기고 user_wallet 은 차감하지 않는다(credit.apply_usage
charge_wallet=False). 사용자의 월 쿼터에 영향을 주지 않는다.

요약 실패(모델 미지정/호출 오류)는 조용히 무시한다 — 제목은 부가 기능이라 대화 자체를 막지 않는다.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from lumen.services import conversation_store as cs
from lumen.services import credit, litellm_client
from lumen.services.providers import routing as ps

logger = logging.getLogger(__name__)

_TITLE_MAX_TOKENS = 32
_TITLE_MAX_CHARS = 80
_TITLE_SYSTEM = (
    "다음 대화를 6단어 이내의 간결한 제목으로 요약하라. "
    "대화의 주 언어를 따르고, 따옴표·마침표·부연 설명 없이 제목만 출력하라."
)


def _clean(text: str) -> str:
    """개행/중복 공백 제거 + 따옴표 정리 + 길이 상한."""
    cleaned = " ".join((text or "").split()).strip().strip("\"'").strip()
    return cleaned[:_TITLE_MAX_CHARS]


def _resp_text(resp) -> str:
    try:
        choices = getattr(resp, "choices", None)
        if choices is None and isinstance(resp, Mapping):
            choices = resp.get("choices")
        first = choices[0]
        msg = getattr(first, "message", None)
        if msg is None and isinstance(first, Mapping):
            msg = first.get("message")
        content = getattr(msg, "content", None)
        if content is None and isinstance(msg, Mapping):
            content = msg.get("content")
        return content or ""
    except Exception:
        return ""


def _resp_usage(resp, model: str, messages: list[dict], text: str) -> tuple[int, int]:
    usage = getattr(resp, "usage", None)
    if usage is None and isinstance(resp, Mapping):
        usage = resp.get("usage")
    return litellm_client.extract_usage(model, messages, text, usage)


async def generate_title_if_absent(*, conversation_id: str, project_id: str, user_id: str) -> None:
    """제목이 없고 요약 모델이 지정돼 있으면 요약·저장(시스템 과금). 모든 실패는 무시."""
    try:
        conv = await cs.get_conversation(conversation_id, user_id=user_id, project_id=project_id)
    except Exception:
        return
    if conv.get("title"):
        return  # 이미 제목 있음(사용자 지정 또는 이전 요약)

    try:
        resolved = await ps.resolve_title_model()
    except Exception:
        logger.warning("제목 요약 모델 조회 실패 conv=%s", conversation_id, exc_info=True)
        return
    if resolved is None:
        return  # 요약 모델 미지정 — 기능 비활성

    try:
        msgs = await cs.list_messages(conversation_id, user_id=user_id, project_id=project_id, limit=6)
    except Exception:
        return
    convo = [m for m in msgs if m.get("role") in ("user", "assistant") and m.get("content")]
    if not convo:
        return
    excerpt = "\n".join(f"{m['role']}: {str(m['content'])[:500]}" for m in convo[:4])
    messages = [
        {"role": "system", "content": _TITLE_SYSTEM},
        {"role": "user", "content": excerpt},
    ]
    model = resolved["model_name"]

    try:
        resp = await litellm_client.acompletion(
            model,
            messages,
            custom_llm_provider=resolved.get("provider_type"),
            api_base=resolved.get("api_base"),
            api_key=resolved.get("api_key"),
            max_tokens=_TITLE_MAX_TOKENS,
            temperature=0.3,
        )
    except Exception:
        logger.warning("제목 요약 호출 실패 conv=%s model=%s", conversation_id, model, exc_info=True)
        return

    title = _clean(_resp_text(resp))
    if not title:
        return

    try:
        await cs.update_title(conversation_id, user_id=user_id, project_id=project_id, title=title)
    except Exception:
        logger.warning("제목 저장 실패 conv=%s", conversation_id, exc_info=True)
        return

    # 시스템 부담 과금 — 원장만 기록, 사용자 지갑 미차감.
    try:
        pt, ct = _resp_usage(resp, model, messages, title)
        usage_cost = litellm_client.cost_from_usage(
            model,
            pt,
            ct,
            input_price_per_token=resolved.get("input_price_per_token"),
            output_price_per_token=resolved.get("output_price_per_token"),
            price_source=resolved.get("price_source"),
            provider_type=resolved.get("provider_type"),
        )
        await credit.apply_usage(
            event_id=f"title:{conversation_id}",
            user_id=user_id,
            project_id=project_id,
            model_name=model,
            provider=resolved.get("provider_name"),
            prompt_tokens=pt,
            completion_tokens=ct,
            usage_cost=usage_cost,
            margin_multiplier=resolved["margin_multiplier"],
            conversation_id=conversation_id,
            source="system",
            charge_wallet=False,
        )
    except Exception:
        logger.warning("제목 요약 시스템 과금 기록 실패 conv=%s", conversation_id, exc_info=True)
