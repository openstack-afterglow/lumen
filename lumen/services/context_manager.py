"""Deterministic bounded context preparation for protocol-v2 model calls."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

_COMPACTION_THRESHOLD = 0.8
_RESERVED_TOKENS = 2_048
_MAX_CHUNKS = 16
_MAX_ROUNDS = 4
_MAX_SUMMARY_CHARS = 16_000


class ContextLimitExceeded(ValueError):
    pass


@dataclass(frozen=True)
class PreparedContext:
    messages: list[dict[str, Any]]
    input_budget: int
    before_tokens: int
    after_tokens: int
    compacted: bool
    source_hashes: tuple[str, ...]


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
    # Conservative deterministic fallback used only by tests and unavailable tokenizers.
    chars = sum(len(_content_text(message)) for message in messages) + len(json.dumps(tool_schemas, sort_keys=True))
    return max(1, (chars + 3) // 4)


def _required_messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    system = [message for message in messages if message.get("role") == "system"]
    non_system = [message for message in messages if message.get("role") != "system"]
    # Retain the active user request and four newest conversational turns verbatim.
    newest = non_system[-5:]
    older = non_system[:-5]
    return system + newest, older


async def prepare_model_messages(
    messages: list[dict[str, Any]],
    tool_schemas: list[dict[str, Any]],
    context_limit: int,
    effective_max_output_tokens: int,
    run_context: dict[str, Any],
    *,
    token_counter: Callable[[list[dict[str, Any]], list[dict[str, Any]]], int] = _default_count,
    compactor: Callable[[list[str], dict[str, Any]], Awaitable[str]] | None = None,
) -> PreparedContext:
    """Preserve mandatory state and compact only the older conversational prefix.

    The caller provides the resolved provider-backed compactor in production.  No
    compaction is silently attempted without that boundary; a bounded failure is
    safer than dropping policy/tool state.
    """
    if context_limit <= 0 or effective_max_output_tokens < 0:
        raise ContextLimitExceeded("context limit is invalid")
    input_budget = context_limit - effective_max_output_tokens - _RESERVED_TOKENS
    if input_budget <= 0:
        raise ContextLimitExceeded("context input budget is nonpositive")
    before_tokens = token_counter(messages, tool_schemas)
    if before_tokens <= int(input_budget * _COMPACTION_THRESHOLD):
        return PreparedContext(
            messages=list(messages),
            input_budget=input_budget,
            before_tokens=before_tokens,
            after_tokens=before_tokens,
            compacted=False,
            source_hashes=(),
        )

    retained, older = _required_messages(messages)
    if not older or compactor is None:
        raise ContextLimitExceeded("context_limit_exceeded")
    chunks: list[str] = []
    chunk_budget = max(1, input_budget // 2)
    current: list[str] = []
    for message in older:
        rendered = json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        candidate = current + [rendered]
        if current and _default_count([{"content": "\n".join(candidate)}], []) > chunk_budget:
            chunks.append("\n".join(current))
            current = [rendered]
        else:
            current = candidate
    if current:
        chunks.append("\n".join(current))
    if len(chunks) > _MAX_CHUNKS:
        raise ContextLimitExceeded("context_limit_exceeded")

    summary = ""
    source_hashes = tuple(_hash(message) for message in older)
    system_count = sum(message.get("role") == "system" for message in retained)
    current_chunks = chunks
    for round_index in range(_MAX_ROUNDS):
        summary = await compactor(current_chunks, {"run_context": run_context, "round": round_index})
        if not isinstance(summary, str) or len(summary) > _MAX_SUMMARY_CHARS:
            raise ContextLimitExceeded("context compaction result is invalid")
        candidate = [
            *retained[:system_count],
            {"role": "user", "content": f"[context summary-data]\n{summary}"},
            *retained[system_count:],
        ]
        after_tokens = token_counter(candidate, tool_schemas)
        if after_tokens <= input_budget:
            return PreparedContext(
                messages=candidate,
                input_budget=input_budget,
                before_tokens=before_tokens,
                after_tokens=after_tokens,
                compacted=True,
                source_hashes=source_hashes,
            )
        if len(current_chunks) == 1:
            break
        current_chunks = [summary]
    raise ContextLimitExceeded("context_limit_exceeded")
