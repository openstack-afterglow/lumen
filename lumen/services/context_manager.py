"""Deterministic, bounded context preparation for model calls."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from lumen.models.chat_contracts import ContextState
from lumen.services.litellm_client import ContextTokenCount, count_context_tokens

_COMPACTION_RECOMMENDATION = 0.70
_COMPACTION_REQUIRED = 0.80
_COMPACTION_TARGET = 0.60
_SAFETY_RESERVE = 2_048
_MAX_CHUNKS = 16
_MAX_ROUNDS = 4
_MAX_SUMMARY_CHARS = 16_000
_MAX_SUMMARY_OUTPUT = 4_096
_SUMMARY_SYSTEM = (
    "Summarize context as user-level reference data; preserve decisions, constraints, reference IDs, "
    "and incomplete requests. Return strict JSON with exactly summary and title. The title must use "
    "the conversation language, contain no more than 6 words, and be at most 80 characters."
)


class ContextLimitExceeded(ValueError):
    """A context operation failed closed with a safe machine-readable code."""

    code = "context_limit_exceeded"

    def __init__(self, message: str = "context_limit_exceeded", *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


@dataclass(frozen=True)
class ContextBudget:
    context_limit: int | None
    output_reserve: int
    safety_reserve: int = _SAFETY_RESERVE

    @property
    def input_budget(self) -> int | None:
        if self.context_limit is None:
            return None
        return self.context_limit - self.output_reserve - self.safety_reserve


@dataclass(frozen=True)
class PreparedContext:
    messages: list[dict[str, Any]]
    input_budget: int
    before_tokens: int
    after_tokens: int
    compacted: bool
    source_hashes: tuple[str, ...]
    measurement: str = "estimated"
    title: str | None = None
    summary: str | None = None
    source_message_ids: tuple[str, ...] = ()


def _content_text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    return json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _hash(message: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(message, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _default_count(messages: list[dict[str, Any]], tool_schemas: list[dict[str, Any]]) -> int:
    chars = sum(len(_content_text(message)) for message in messages) + len(
        json.dumps(tool_schemas, sort_keys=True, ensure_ascii=False, default=str)
    )
    return max(1, (chars + 3) // 4)


def _count_result(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]], counter: Callable
) -> tuple[int | None, str]:
    result = counter(messages, tools)
    if isinstance(result, ContextTokenCount):
        return result.tokens, result.measurement
    if isinstance(result, int):
        return max(0, result), "estimated"
    tokens = getattr(result, "tokens", None)
    measurement = getattr(result, "measurement", "estimated")
    return (max(0, int(tokens)), measurement) if tokens is not None else (None, "unknown")


def _required_messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep instructions and the last two user turns, never splitting tool groups."""
    instructions = [m for m in messages if m.get("role") in {"system", "developer"}]
    body = [m for m in messages if m.get("role") not in {"system", "developer"}]
    user_positions = [index for index, message in enumerate(body) if message.get("role") == "user"]
    if not user_positions:
        return instructions + body, []
    start = user_positions[max(0, len(user_positions) - 2)]
    # A retained suffix can begin at a tool result only when malformed input was
    # supplied. Expand backwards in that case and keep the assistant call too.
    while start > 0 and body[start].get("role") == "tool":
        start -= 1
    if start > 0 and body[start].get("role") == "assistant" and body[start].get("tool_calls"):
        start -= 1
    return instructions + body[start:], body[:start]


def _parse_compaction_result(value: Any) -> tuple[str, str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContextLimitExceeded("compaction_failed", code="compaction_failed") from exc
    if not isinstance(value, dict) or set(value) not in ({"summary", "title"}, {"summary", "title", "usage"}):
        raise ContextLimitExceeded("compaction_failed", code="compaction_failed")
    if "usage" in value and not isinstance(value["usage"], dict):
        raise ContextLimitExceeded("compaction_failed", code="compaction_failed")
    summary, title = value.get("summary"), value.get("title")
    if not isinstance(summary, str) or not isinstance(title, str):
        raise ContextLimitExceeded("compaction_failed", code="compaction_failed")
    if not summary.strip() or len(summary) > _MAX_SUMMARY_CHARS or len(title) > 80:
        raise ContextLimitExceeded("compaction_failed", code="compaction_failed")
    return summary, title


def context_state(
    messages: list[dict[str, Any]],
    tool_schemas: list[dict[str, Any]],
    *,
    model_name: str,
    context_limit: int | None,
    output_reserve: int,
    revision: str,
    checkpoint_id: str | None = None,
    active_compaction_run_id: str | None = None,
) -> ContextState:
    """Build the honest, non-provider-calling context status projection."""
    budget = ContextBudget(context_limit, max(0, output_reserve))
    count = count_context_tokens(model_name, messages, tool_schemas)
    input_budget = budget.input_budget
    _retained, older = _required_messages(messages)
    if input_budget is None or input_budget <= 0 or count.tokens is None:
        return ContextState(
            model_name=model_name,
            context_limit=context_limit,
            output_reserve=budget.output_reserve,
            safety_reserve=budget.safety_reserve,
            input_budget=input_budget,
            input_tokens=count.tokens,
            utilization=None,
            measurement=count.measurement if input_budget is not None else "unknown",
            recommendation="unavailable",
            can_compact=False,
            reason_code="context_budget_unavailable",
            revision=revision,
            checkpoint_id=checkpoint_id,
            active_compaction_run_id=active_compaction_run_id,
        )
    utilization = count.tokens / input_budget
    recommendation = (
        "required"
        if utilization >= _COMPACTION_REQUIRED
        else "compact"
        if utilization >= _COMPACTION_RECOMMENDATION
        else "none"
    )
    reason = "context_limit_exceeded" if count.tokens > input_budget and not older else None
    return ContextState(
        model_name=model_name,
        context_limit=context_limit,
        output_reserve=budget.output_reserve,
        safety_reserve=budget.safety_reserve,
        input_budget=input_budget,
        input_tokens=count.tokens,
        utilization=utilization,
        measurement=count.measurement,
        recommendation=recommendation,
        can_compact=bool(older),
        reason_code=reason,
        revision=revision,
        checkpoint_id=checkpoint_id,
        active_compaction_run_id=active_compaction_run_id,
    )


def _summary_messages(chunks: list[str], *, system_prompt: str) -> list[dict[str, str]]:
    framed = json.dumps(chunks, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return [{"role": "system", "content": system_prompt}, {"role": "user", "content": framed}]


def _safe_prefix(text: str, length: int) -> str:
    result = text[:length]
    if result and 0xD800 <= ord(result[-1]) <= 0xDBFF:
        result = result[:-1]
    return result


def _render_message(message: dict[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _summary_counter(
    run_context: dict[str, Any],
    fallback: Callable[[list[dict[str, Any]], list[dict[str, Any]]], Any],
) -> Callable[[list[dict[str, Any]], list[dict[str, Any]]], Any]:
    counter = run_context.get("summary_token_counter")
    return counter if callable(counter) else fallback


def _chunk_messages(
    older: list[dict[str, Any]],
    *,
    counter: Callable,
    summary_limit: int,
    summary_output: int,
    system_prompt: str,
) -> list[str]:
    chunk_budget = summary_limit - summary_output - _SAFETY_RESERVE
    if chunk_budget <= 0:
        raise ContextLimitExceeded("context input budget is nonpositive")

    def fits(lines: list[str]) -> bool:
        count, _ = _count_result(_summary_messages(lines, system_prompt=system_prompt), [], counter)
        return count is not None and count <= chunk_budget

    chunks: list[str] = []
    current: list[str] = []
    for message in older:
        rendered = _render_message(message)
        if fits([*current, rendered]):
            current.append(rendered)
            continue
        if current:
            chunks.append("\n".join(current))
            current = []
        if fits([rendered]):
            current = [rendered]
            continue
        # A single giant text turn must be split at a safe character/token
        # boundary; non-text provider blocks cannot be safely divided.
        content = message.get("content")
        if not isinstance(content, str):
            raise ContextLimitExceeded("compaction_failed", code="compaction_failed")
        lo, hi = 1, len(content)
        best = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            fragment = dict(message)
            fragment["content"] = _safe_prefix(content, mid)
            if fits([_render_message(fragment)]):
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        if best <= 0:
            raise ContextLimitExceeded("compaction_failed", code="compaction_failed")
        offset = 0
        while offset < len(content):
            lo, hi, best = offset + 1, len(content), offset
            while lo <= hi:
                mid = (lo + hi) // 2
                fragment = dict(message)
                fragment["content"] = _safe_prefix(content[offset:mid], mid - offset)
                if fits([_render_message(fragment)]):
                    best = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            if best <= offset:
                raise ContextLimitExceeded("compaction_failed", code="compaction_failed")
            fragment = dict(message)
            fragment["content"] = content[offset:best]
            chunks.append(_render_message(fragment))
            offset = best
    if current:
        chunks.append("\n".join(current))
    if not chunks or len(chunks) > _MAX_CHUNKS:
        raise ContextLimitExceeded("compaction_failed", code="compaction_failed")
    return chunks


def _split_summary_text(
    text: str,
    *,
    counter: Callable,
    summary_budget: int,
    system_prompt: str,
) -> list[str]:
    """Split one oversized map result while rechecking the complete framing."""
    if not text:
        raise ContextLimitExceeded("compaction_failed", code="compaction_failed")

    def fits(fragment: str) -> bool:
        count, _ = _count_result(_summary_messages([fragment], system_prompt=system_prompt), [], counter)
        return count is not None and count <= summary_budget

    if fits(text):
        return [text]
    pieces: list[str] = []
    offset = 0
    while offset < len(text):
        lo, hi, best = offset + 1, len(text), offset
        while lo <= hi:
            mid = (lo + hi) // 2
            fragment = _safe_prefix(text[offset:mid], mid - offset)
            if fits(fragment):
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        if best <= offset:
            raise ContextLimitExceeded("compaction_failed", code="compaction_failed")
        pieces.append(text[offset:best])
        offset = best
    return pieces


def _candidate_messages(retained: list[dict[str, Any]], summary: str) -> list[dict[str, Any]]:
    instructions = [m for m in retained if m.get("role") in {"system", "developer"}]
    body = [m for m in retained if m.get("role") not in {"system", "developer"}]
    return [*instructions, {"role": "user", "content": f"[context summary-data]\n{summary}"}, *body]


def _title_context(retained: list[dict[str, Any]], run_context: dict[str, Any]) -> dict[str, Any]:
    stored = run_context.get("stored_messages")
    if isinstance(stored, list) and all(isinstance(item, dict) for item in stored):
        retained_for_title = stored
    else:
        count = run_context.get("stored_message_count")
        if isinstance(count, int) and count >= 0:
            body = [m for m in retained if m.get("role") not in {"system", "developer"}]
            retained_for_title = body[:count]
        else:
            retained_for_title = retained
    return {
        "previous_summary": str(run_context.get("previous_summary") or ""),
        "retained_messages": retained_for_title,
    }


async def prepare_model_messages(
    messages: list[dict[str, Any]],
    tool_schemas: list[dict[str, Any]],
    context_limit: int,
    effective_max_output_tokens: int,
    run_context: dict[str, Any],
    *,
    token_counter: Callable[[list[dict[str, Any]], list[dict[str, Any]]], int | ContextTokenCount] = _default_count,
    compactor: Callable[[list[str], dict[str, Any]], Awaitable[Any]] | None = None,
) -> PreparedContext:
    """Prepare provider messages, failing closed whenever a known budget is unsafe."""
    budget = ContextBudget(context_limit, effective_max_output_tokens)
    if context_limit <= 0 or effective_max_output_tokens < 0 or budget.input_budget is None or budget.input_budget <= 0:
        raise ContextLimitExceeded("context input budget is nonpositive")
    before_tokens, measurement = _count_result(messages, tool_schemas, token_counter)
    if before_tokens is None:
        raise ContextLimitExceeded("context_budget_unavailable", code="context_budget_unavailable")

    retained, older = _required_messages(messages)
    required_tokens, _ = _count_result(retained, tool_schemas, token_counter)
    if required_tokens is None:
        raise ContextLimitExceeded("context_budget_unavailable", code="context_budget_unavailable")
    if required_tokens > budget.input_budget:
        raise ContextLimitExceeded("context_limit_exceeded")
    force = bool(run_context.get("force_compaction"))
    automatic = before_tokens >= budget.input_budget * _COMPACTION_REQUIRED
    if before_tokens <= budget.input_budget and not force and not automatic:
        return PreparedContext(
            list(messages), budget.input_budget, before_tokens, before_tokens, False, (), measurement
        )
    if not older:
        if before_tokens <= budget.input_budget:
            return PreparedContext(
                list(messages), budget.input_budget, before_tokens, before_tokens, False, (), measurement
            )
        raise ContextLimitExceeded("context_limit_exceeded")
    if compactor is None:
        raise ContextLimitExceeded("context_limit_exceeded", code="compaction_failed")

    summary_limit_value = run_context.get("summary_context_limit", context_limit)
    try:
        summary_limit = int(summary_limit_value)
    except (TypeError, ValueError):
        raise ContextLimitExceeded("context_budget_unavailable", code="context_budget_unavailable") from None
    configured_summary_output = run_context.get("summary_output_reserve")
    try:
        summary_output = min(
            _MAX_SUMMARY_OUTPUT,
            max(
                1,
                int(
                    configured_summary_output if configured_summary_output is not None else effective_max_output_tokens
                ),
            ),
        )
    except (TypeError, ValueError):
        raise ContextLimitExceeded("context_budget_unavailable", code="context_budget_unavailable") from None
    system_prompt = str(run_context.get("summary_system_prompt") or _SUMMARY_SYSTEM)
    summary_counter = _summary_counter(run_context, token_counter)
    chunks = _chunk_messages(
        older,
        counter=summary_counter,
        summary_limit=summary_limit,
        summary_output=summary_output,
        system_prompt=system_prompt,
    )
    try:
        checkpoint_raw_prefix_count = int(run_context.get("checkpoint_raw_prefix_count"))
    except (TypeError, ValueError):
        checkpoint_raw_prefix_count = None
    try:
        summary_projection_count = int(run_context.get("summary_projection_count", 1))
    except (TypeError, ValueError):
        summary_projection_count = 1
    supplied_hashes = tuple(str(item) for item in run_context.get("source_hashes", ()))
    supplied_ids = tuple(str(item) for item in run_context.get("source_message_ids", ()))
    if supplied_hashes:
        if checkpoint_raw_prefix_count is not None:
            if checkpoint_raw_prefix_count < 0 or summary_projection_count < 0:
                raise ContextLimitExceeded("compaction_failed", code="compaction_failed")
            prefix_count = checkpoint_raw_prefix_count + max(0, len(older) - summary_projection_count)
        else:
            prefix_count = len(older)
        available = min(len(supplied_hashes), len(supplied_ids) if supplied_ids else len(supplied_hashes))
        if prefix_count <= 0 or prefix_count > available:
            raise ContextLimitExceeded("compaction_failed", code="compaction_failed")
        source_hashes = supplied_hashes[:prefix_count]
        source_ids = supplied_ids[:prefix_count]
    else:
        source_hashes = tuple(_hash(message) for message in older)
        source_ids = tuple(str(message["id"]) for message in older if message.get("id") is not None)
    title_context = _title_context(retained, run_context)

    parts: list[str] = []
    map_titles: list[str] = []
    for chunk_index, chunk in enumerate(chunks):
        framed_map_count, _ = _count_result(
            _summary_messages([chunk], system_prompt=system_prompt), [], summary_counter
        )
        if framed_map_count is None or framed_map_count > summary_limit - summary_output - _SAFETY_RESERVE:
            raise ContextLimitExceeded("compaction_failed", code="compaction_failed")
        result = await compactor(
            [chunk],
            {
                "run_context": run_context,
                "round": 0,
                "chunk_index": chunk_index,
                "phase": "map",
                "summary_messages": _summary_messages([chunk], system_prompt=system_prompt),
                "title_context": title_context,
            },
        )
        part, title = _parse_compaction_result(result)
        parts.append(part)
        map_titles.append(title)
    previous_summary = str(title_context.get("previous_summary") or "")
    if previous_summary:
        parts.insert(0, f"[previous context summary]\n{previous_summary}")

    title = map_titles[-1] if map_titles else ""
    summary = ""
    for reduce_round in range(1, _MAX_ROUNDS + 1):
        # Each reduce call receives all source summaries that fit its own fully
        # framed route window. Order is never changed and no summary is dropped.
        summary_budget = summary_limit - summary_output - _SAFETY_RESERVE
        if summary_budget <= 0:
            raise ContextLimitExceeded("context input budget is nonpositive")
        repacked: list[str] = []
        for part in parts:
            repacked.extend(
                _split_summary_text(
                    part,
                    counter=summary_counter,
                    summary_budget=summary_budget,
                    system_prompt=system_prompt,
                )
            )
        if len(repacked) > _MAX_CHUNKS:
            raise ContextLimitExceeded("compaction_failed", code="compaction_failed")
        # Each reduce call receives all source summaries that fit its own fully
        # framed route window. Order is never changed and no summary is dropped.
        reduce_inputs: list[list[str]] = []
        batch: list[str] = []
        for part in repacked:
            candidate = [*batch, part]
            count, _ = _count_result(_summary_messages(candidate, system_prompt=system_prompt), [], summary_counter)
            if batch and (count is None or count > summary_budget):
                reduce_inputs.append(batch)
                batch = [part]
            else:
                batch = candidate
        if batch:
            reduce_inputs.append(batch)
        if not reduce_inputs or len(reduce_inputs) > _MAX_CHUNKS:
            raise ContextLimitExceeded("compaction_failed", code="compaction_failed")

        reduced: list[str] = []
        final_title = title
        for chunk_index, reduce_input in enumerate(reduce_inputs):
            is_final = len(reduce_inputs) == 1
            framed_reduce_count, _ = _count_result(
                _summary_messages(reduce_input, system_prompt=system_prompt), [], summary_counter
            )
            if framed_reduce_count is None or framed_reduce_count > summary_budget:
                raise ContextLimitExceeded("compaction_failed", code="compaction_failed")
            result = await compactor(
                reduce_input,
                {
                    "run_context": run_context,
                    "round": reduce_round,
                    "chunk_index": chunk_index,
                    "phase": "reduce",
                    "final": is_final,
                    "summary_messages": _summary_messages(reduce_input, system_prompt=system_prompt),
                    "title_context": title_context,
                },
            )
            reduced_summary, reduced_title = _parse_compaction_result(result)
            reduced.append(reduced_summary)
            if is_final:
                final_title = reduced_title
        parts = reduced
        summary = "\n".join(parts)
        if len(summary) > _MAX_SUMMARY_CHARS:
            if reduce_round == _MAX_ROUNDS:
                raise ContextLimitExceeded("compaction_failed", code="compaction_failed")
            continue
        candidate_messages = _candidate_messages(retained, summary)
        after_tokens, _ = _count_result(candidate_messages, tool_schemas, token_counter)
        target_tokens = max(required_tokens, int(budget.input_budget * _COMPACTION_TARGET))
        if (
            after_tokens is not None
            and after_tokens <= budget.input_budget
            and after_tokens < before_tokens
            and (after_tokens <= target_tokens or reduce_round == _MAX_ROUNDS)
        ):
            return PreparedContext(
                candidate_messages,
                budget.input_budget,
                before_tokens,
                after_tokens,
                True,
                source_hashes,
                measurement,
                final_title,
                summary,
                source_ids,
            )
        if reduce_round == _MAX_ROUNDS:
            break
    raise ContextLimitExceeded("context_limit_exceeded")
