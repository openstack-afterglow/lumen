"""Pure first-exchange title generation for the durable title job.

This module deliberately has no database side effects.  The title job owns the
transaction which persists the generated title and its system usage record.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from lumen.services import litellm_client

logger = logging.getLogger(__name__)

_TITLE_MAX_TOKENS = 512
_TITLE_MAX_CHARS = 80
_TITLE_MAX_WORDS = 6
_TITLE_SYSTEM = (
    "Summarize this first user request and the successful assistant answer as a concise "
    "conversation title. Use the conversation's primary language. Return only a title, "
    "at most 6 words and 80 characters, without quotes, punctuation, or explanation."
)
_OMISSION = "[…생략…]"


@dataclass(frozen=True)
class TitleResult:
    title: str
    prompt_tokens: int
    completion_tokens: int
    messages: list[dict[str, Any]]
    model_name: str


def _clean(text: str) -> str:
    """Normalize model output to the public title limits."""
    cleaned = " ".join((text or "").split()).strip().strip("\"'").strip()
    # Providers occasionally wrap a valid title in a JSON object despite the
    # plain-text instruction.  Accept only the title field, never arbitrary
    # provider metadata or a summary prompt.
    try:
        value = json.loads(cleaned)
    except (TypeError, ValueError, json.JSONDecodeError):
        value = None
    if isinstance(value, Mapping) and isinstance(value.get("title"), str):
        cleaned = " ".join(value["title"].split()).strip().strip("\"'").strip()
    words = cleaned.split()
    if len(words) > _TITLE_MAX_WORDS:
        cleaned = " ".join(words[:_TITLE_MAX_WORDS])
    return cleaned[:_TITLE_MAX_CHARS].rstrip()


def _resp_text(resp: Any) -> str:
    try:
        choices = getattr(resp, "choices", None)
        if choices is None and isinstance(resp, Mapping):
            choices = resp.get("choices")
        first = choices[0]
        message = getattr(first, "message", None)
        if message is None and isinstance(first, Mapping):
            message = first.get("message")
        content = getattr(message, "content", None)
        if content is None and isinstance(message, Mapping):
            content = message.get("content")
        return content if isinstance(content, str) else ""
    except (AttributeError, IndexError, KeyError, TypeError):
        return ""


def _resp_usage(resp: Any, model: str, messages: list[dict[str, Any]], text: str) -> tuple[int, int]:
    usage = getattr(resp, "usage", None)
    if usage is None and isinstance(resp, Mapping):
        usage = resp.get("usage")
    return litellm_client.extract_usage(model, messages, text, usage)


def _route_capabilities(route: Mapping[str, Any]) -> Mapping[str, Any]:
    capabilities = route.get("capabilities")
    return capabilities if isinstance(capabilities, Mapping) else {}


def _context_limit(route: Mapping[str, Any]) -> int | None:
    capabilities = _route_capabilities(route)
    value = capabilities.get("context_limit", route.get("context_limit"))
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _fit_text(model: str, role: str, text: str, token_budget: int) -> str:
    """Keep role text within a tokenizer budget while preserving both ends."""
    if token_budget <= 0:
        return ""
    original = text or ""
    if litellm_client.count_tokens(model, messages=[{"role": role, "content": original}]) <= token_budget:
        return original

    def candidate(keep: int) -> str:
        if keep <= 0:
            return _OMISSION
        front = max(1, (keep * 3) // 4)
        tail = max(1, keep - front)
        return f"{original[:front]} {_OMISSION} {original[-tail:]}"

    # Find the largest source-character bound whose marker-inclusive form fits.
    lo, hi = 0, max(1, len(original))
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if litellm_client.count_tokens(model, messages=[{"role": role, "content": candidate(mid)}]) <= token_budget:
            lo = mid
        else:
            hi = mid - 1
    fitted = candidate(lo)
    return (
        fitted
        if litellm_client.count_tokens(model, messages=[{"role": role, "content": fitted}]) <= token_budget
        else ""
    )


def _fit_messages_to_budget(*, model: str, messages: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    """Fit both exchange roles after reserving system and role framing tokens."""
    if litellm_client.count_tokens(model, messages=messages) <= budget:
        return messages
    system = messages[0]
    roles = messages[1:]
    system_tokens = litellm_client.count_tokens(model, messages=[system])
    if system_tokens > budget:
        raise ValueError("title context budget exceeded")
    empty_roles = [system, *({"role": item["role"], "content": ""} for item in roles)]
    framing_tokens = max(
        0,
        litellm_client.count_tokens(model, messages=empty_roles) - system_tokens,
    )
    available = max(0, budget - system_tokens - framing_tokens)
    # Allocate the remaining content budget equally before any role-specific trim.
    high = available // max(1, len(roles))

    def build(role_budget: int) -> list[dict[str, Any]]:
        if role_budget <= 0:
            return [system, *({"role": item["role"], "content": ""} for item in roles)]
        return [
            system,
            *(
                {
                    "role": item["role"],
                    "content": _fit_text(model, item["role"], str(item["content"]), role_budget),
                }
                for item in roles
            ),
        ]

    candidate = build(high)
    if litellm_client.count_tokens(model, messages=candidate) <= budget:
        return candidate
    # Tokenizer framing can differ from the estimate above.  Validate the actual
    # complete request and reduce both roles together, retaining the 50/50 split.
    lo = 0
    best: list[dict[str, Any]] | None = None
    while lo <= high:
        mid = (lo + high) // 2
        current = build(mid)
        if litellm_client.count_tokens(model, messages=current) <= budget:
            best = current
            lo = mid + 1
        else:
            high = mid - 1
    if best is None:
        best = build(0)
        if litellm_client.count_tokens(model, messages=best) > budget:
            raise ValueError("title context budget exceeded")
    return best


def build_title_messages(*, exchange: Sequence[Mapping[str, Any]], route: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Build the one first-exchange request, truncating only when its window requires it."""
    model = str(route.get("model_name") or "")
    roles: list[tuple[str, str]] = []
    for item in exchange:
        role = item.get("role")
        content = item.get("content")
        if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
            roles.append((str(role), content))
    # The enqueue snapshot is exactly one user/assistant exchange.  If a caller
    # supplies extras, retain the first user and its following assistant only.
    user = next(((r, c) for r, c in roles if r == "user"), None)
    assistant = next(((r, c) for i, (r, c) in enumerate(roles) if r == "assistant" and i > 0), None)
    if user is None or assistant is None:
        return [{"role": "system", "content": _TITLE_SYSTEM}]
    messages = [
        {"role": "system", "content": _TITLE_SYSTEM},
        {"role": "user", "content": user[1]},
        {"role": "assistant", "content": assistant[1]},
    ]
    limit = _context_limit(route)
    if limit is None:
        return messages
    # Reserve the title output and standard safety reserve, then account for
    # system and role framing before splitting the remaining budget 50/50.
    budget = max(1, limit - _TITLE_MAX_TOKENS - 2048)
    return _fit_messages_to_budget(model=model, messages=messages, budget=budget)


async def generate_title(*, exchange: Sequence[Mapping[str, Any]], route: Mapping[str, Any]) -> TitleResult:
    """Call the frozen title route once and return title plus observed usage."""
    model = str(route.get("model_name") or "")
    if not model:
        raise ValueError("title model is unavailable")
    messages = build_title_messages(exchange=exchange, route=route)
    if len(messages) < 3:
        raise ValueError("first exchange is incomplete")
    limit = _context_limit(route)
    if limit is not None:
        budget = max(1, limit - _TITLE_MAX_TOKENS - 2048)
        if litellm_client.count_tokens(model, messages=messages) > budget:
            raise ValueError("title context budget exceeded")
    params: dict[str, Any] = {
        "custom_llm_provider": route.get("provider_type"),
        "api_base": route.get("api_base"),
        "api_key": route.get("api_key"),
        "max_tokens": _TITLE_MAX_TOKENS,
        "temperature": 0.0,
    }
    params = {key: value for key, value in params.items() if value is not None}
    # none is the least expensive supported reasoning mode.  The wrapper
    # omits it for providers which do not support reasoning parameters.
    reasoning_params = getattr(litellm_client, "_reasoning_params", None)
    if callable(reasoning_params):
        params.update(reasoning_params(model, "none", route.get("provider_type")))
    response = await litellm_client.acompletion(model, messages, **params)
    raw = _resp_text(response)
    title = _clean(raw)
    if not title:
        raise ValueError("title model returned an empty title")
    prompt_tokens, completion_tokens = _resp_usage(response, model, messages, raw)
    return TitleResult(
        title=title,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        messages=messages,
        model_name=model,
    )


# Explicit aliases make the pure boundary easy for callers/tests to discover.
clean_title = _clean
