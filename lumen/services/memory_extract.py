"""Post-response long-term-memory extraction through the designated small model.

The model receives visible encrypted-memory plaintext plus a bounded finished
conversation slice and returns only validated add/update/delete deltas.  It
never receives authority to alter another user's memory; application code
serializes one user/project and rechecks each referenced ID before mutation.

Extraction is supplemental: malformed output and provider failure do not
affect the completed chat response.  Usage is system-funded.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Mapping

from lumen.services import conversation_store as cs
from lumen.services import credit, litellm_client
from lumen.services import memory_store as ms
from lumen.services.providers import routing as ps

logger = logging.getLogger(__name__)

_MAX_TOKENS = 400
_MAX_EXISTING = 30  # 프롬프트에 넣는 기존 메모리 상한(비용)
_MAX_OPS = 6  # 한 번에 반영할 op 상한(runaway 방지)
_MAX_CONTENT_CHARS = 2000
_CATEGORIES = frozenset({"interest", "development", "habit", "preference", "general"})


class MemoryExtractionRetryable(RuntimeError):
    """A provider or source-of-truth failure that must requeue the durable job."""


_SYSTEM = (
    "당신은 사용자의 장기 메모리를 관리하는 소형 분류 모델이다. [기존 메모리]와 완료된 [최근 세션]을 "
    "비교해 안정적인 사용자 사실의 델타만 JSON 배열로 출력하라. 허용 category 는 "
    "interest(관심사), development(현재 개발 특성), habit(습관), preference(선호), general 이다. "
    "각 항목은 다음 중 하나여야 한다:\n"
    '- {"op":"add","category":"preference","content":"새로 기억할 한 문장"}\n'
    '- {"op":"update","id":<기존 메모리 id>,"category":"habit","content":"갱신된 한 문장"}\n'
    '- {"op":"delete","id":<기존 메모리 id>}\n'
    "사용자가 명시한 안정적인 관심사·개발 방식·습관·선호만 기록하고, 모델/도구/보조자의 발언을 사실로 "
    "삼지 마라. update 로 모순되거나 더 이상 유효하지 않은 항목을 대체하고, delete 는 사용자가 "
    "분명히 철회했거나 모순되어 대체할 수 없는 기존 항목에만 사용하라. 기존 사실을 중복 add 하지 말고, "
    "일시적 요청·민감 정보·추측은 제외하라. 새 델타가 없으면 []만 출력하라. 설명이나 마크다운은 금지한다."
)


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


def _parse_ops(text: str) -> list[dict]:
    """모델 출력에서 JSON 배열을 방어적으로 추출·파싱. 실패 시 빈 목록."""
    if not text:
        return []
    # ```json ... ``` 펜스나 잡음 제거 — 첫 '[' ~ 마지막 ']' 구간만.
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return []
    try:
        parsed = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _normalize_ops(ops: list[dict], existing: dict[int, dict]) -> list[dict]:
    normalized: list[dict] = []
    referenced_ids: set[int] = set()
    added_contents: set[str] = set()
    for op in ops[:_MAX_OPS]:
        if not isinstance(op, dict):
            continue
        kind = op.get("op")
        if kind == "add":
            content = op.get("content")
            category = op.get("category")
            if isinstance(content, str) and content.strip() and isinstance(category, str) and category in _CATEGORIES:
                normalized_content = content.strip()[:_MAX_CONTENT_CHARS]
                content_key = normalized_content.casefold()
                if content_key not in added_contents:
                    added_contents.add(content_key)
                    normalized.append({"op": "add", "category": category, "content": normalized_content})
            continue
        memory_id = op.get("id")
        snapshot = existing.get(memory_id) if isinstance(memory_id, int) else None
        if (
            kind not in {"update", "delete"}
            or snapshot is None
            or memory_id in referenced_ids
            or not isinstance(snapshot.get("content"), str)
            or not isinstance(snapshot.get("category"), str)
            or not isinstance(snapshot.get("status"), str)
            or not isinstance(snapshot.get("is_active"), bool)
        ):
            continue
        referenced_ids.add(memory_id)
        expected_state_fingerprint = ms.memory_state_fingerprint(
            snapshot["content"],
            category=snapshot["category"],
            status=snapshot["status"],
            is_active=snapshot["is_active"],
        )
        if kind == "delete":
            normalized.append(
                {
                    "op": "delete",
                    "id": memory_id,
                    "expected_state_fingerprint": expected_state_fingerprint,
                }
            )
            continue
        content = op.get("content")
        category = op.get("category")
        if isinstance(content, str) and content.strip() and isinstance(category, str) and category in _CATEGORIES:
            normalized.append(
                {
                    "op": "update",
                    "id": memory_id,
                    "category": category,
                    "content": content.strip()[:_MAX_CONTENT_CHARS],
                    "expected_state_fingerprint": expected_state_fingerprint,
                }
            )
    return normalized


async def generate_memory_if_applicable(
    *,
    conversation_id: str,
    project_id: str,
    run_id: str,
    user_id: str,
) -> list[dict] | None:
    """Return validated deltas; only no-model/malformed output is a terminal no-op."""
    try:
        resolved = await ps.resolve_memory_model()
    except Exception as exc:
        raise MemoryExtractionRetryable("memory model resolution failed") from exc
    if resolved is None:
        return None

    try:
        existing = await ms.list_memories(user_id=user_id, project_id=project_id)
        msgs = await cs.list_messages_for_run(run_id, user_id=user_id, project_id=project_id)
    except Exception as exc:
        raise MemoryExtractionRetryable("memory source loading failed") from exc
    active = [
        memory
        for memory in existing
        if (
            memory.get("is_active") is True
            and memory.get("scope") == "project"
            and memory.get("project_id") == project_id
            and memory.get("status") == "active"
        )
    ][:_MAX_EXISTING]
    existing_by_id = {memory["id"]: memory for memory in active if isinstance(memory.get("id"), int)}
    user_messages = [message for message in msgs if message.get("role") == "user" and message.get("content")]
    if not user_messages:
        return None

    existing_block = (
        "\n".join(
            f"- (id {memory['id']}, {memory.get('category', 'general')}) {str(memory['content'])[:300]}"
            for memory in active
        )
        if active
        else "(없음)"
    )
    session_block = "\n".join(
        f"{message['role']}: {str(message['content'])[:500]}"
        for message in msgs
        if message.get("role") in {"user", "assistant"} and message.get("content")
    )
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": f"[기존 메모리]\n{existing_block}\n\n[최근 세션]\n{session_block}"},
    ]
    model = resolved["model_name"]

    try:
        response = await litellm_client.acompletion(
            model,
            messages,
            custom_llm_provider=resolved.get("provider_type"),
            api_base=resolved.get("api_base"),
            api_key=resolved.get("api_key"),
            max_tokens=_MAX_TOKENS,
            temperature=0.2,
        )
    except Exception as exc:
        raise MemoryExtractionRetryable("memory model request failed") from exc

    text = _resp_text(response)
    ops = _normalize_ops(_parse_ops(text), existing_by_id)

    # System-funded accounting is independent of the durable memory mutation.
    # A ledger-write failure must not repeat a successfully received model call.
    try:
        usage = getattr(response, "usage", None)
        if usage is None and isinstance(response, Mapping):
            usage = response.get("usage")
        prompt_tokens, completion_tokens = litellm_client.extract_usage(model, messages, text, usage)
        usage_cost = litellm_client.cost_from_usage(
            model,
            prompt_tokens,
            completion_tokens,
            input_price_per_token=resolved.get("input_price_per_token"),
            output_price_per_token=resolved.get("output_price_per_token"),
            price_source=resolved.get("price_source"),
            provider_type=resolved.get("provider_type"),
        )
        await credit.apply_usage(
            event_id=f"memory:{conversation_id}:{uuid.uuid4().hex}",
            user_id=user_id,
            project_id=project_id,
            model_name=model,
            provider=resolved.get("provider_name"),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            usage_cost=usage_cost,
            margin_multiplier=resolved["margin_multiplier"],
            conversation_id=conversation_id,
            source="system",
            charge_wallet=False,
        )
    except Exception:
        logger.warning("메모리 추출 시스템 과금 기록 실패 conv=%s", conversation_id, exc_info=True)
    return ops
