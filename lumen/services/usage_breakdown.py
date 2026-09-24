"""Canonical token breakdown shared by every billing path.

The ledger follows the Anthropic organization usage report: uncached input,
cache read, cache creation (5 minute and 1 hour TTL) and output. Providers and
transports report those categories in incompatible shapes, so every billing
path converts its raw usage here exactly once.

Invariants:
- ``input_tokens`` is TOTAL input, cache read and cache creation included. It is
  what ``chat_usage_logs.prompt_tokens`` stores and what context occupancy and
  the ``usage.prompt_tokens`` returned to API callers mean.
- ``uncached_input_tokens`` is derived and never negative.
- When cache creation is known but its TTL split is missing, all of it is
  attributed to the 5 minute bucket (Anthropic's default TTL).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from lumen.services import native_compaction

CACHE_USAGE_KEYS = (
    "cache_read_input_tokens",
    "cache_creation_5m_input_tokens",
    "cache_creation_1h_input_tokens",
)


def _count(value: object) -> int | None:
    """Return a non-negative int, rejecting bools, negatives and non-integers."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _field(source: Any, key: str) -> Any:
    if source is None:
        return None
    if isinstance(source, Mapping):
        return source.get(key)
    return getattr(source, key, None)


def _split_creation(total: int | None, reported_1h: int | None, reported_5m: int | None) -> tuple[int, int]:
    """Return ``(5m, 1h)`` for one cache creation total."""
    if total is None:
        # Only the split was reported; its sum is the creation total.
        return (reported_5m or 0), (reported_1h or 0)
    one_hour = min(reported_1h, total) if reported_1h is not None else 0
    return total - one_hour, one_hour


@dataclass(frozen=True)
class UsageBreakdown:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_5m_input_tokens: int = 0
    cache_creation_1h_input_tokens: int = 0

    @property
    def cache_creation_input_tokens(self) -> int:
        return self.cache_creation_5m_input_tokens + self.cache_creation_1h_input_tokens

    @property
    def uncached_input_tokens(self) -> int:
        return max(0, self.input_tokens - self.cache_read_input_tokens - self.cache_creation_input_tokens)

    @property
    def has_cache(self) -> bool:
        return bool(self.cache_read_input_tokens or self.cache_creation_input_tokens)

    def cache_fields(self) -> dict[str, int]:
        return {
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_5m_input_tokens": self.cache_creation_5m_input_tokens,
            "cache_creation_1h_input_tokens": self.cache_creation_1h_input_tokens,
        }

    def as_usage_dict(self) -> dict[str, int]:
        """Runtime usage dict: ``prompt_tokens`` stays total input."""
        return {"prompt_tokens": self.input_tokens, "completion_tokens": self.output_tokens, **self.cache_fields()}

    def __add__(self, other: UsageBreakdown) -> UsageBreakdown:
        return UsageBreakdown(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
            cache_creation_5m_input_tokens=self.cache_creation_5m_input_tokens + other.cache_creation_5m_input_tokens,
            cache_creation_1h_input_tokens=self.cache_creation_1h_input_tokens + other.cache_creation_1h_input_tokens,
        )

    @classmethod
    def from_totals(
        cls,
        input_tokens: int,
        output_tokens: int,
        *,
        cache_read_input_tokens: object = 0,
        cache_creation_5m_input_tokens: object = 0,
        cache_creation_1h_input_tokens: object = 0,
    ) -> UsageBreakdown:
        """Build a clamped breakdown whose cache never exceeds total input."""
        total = max(0, int(input_tokens))
        read = min(_count(cache_read_input_tokens) or 0, total)
        one_hour = min(_count(cache_creation_1h_input_tokens) or 0, total - read)
        five_minute = min(_count(cache_creation_5m_input_tokens) or 0, total - read - one_hour)
        return cls(
            input_tokens=total,
            output_tokens=max(0, int(output_tokens)),
            cache_read_input_tokens=read,
            cache_creation_5m_input_tokens=five_minute,
            cache_creation_1h_input_tokens=one_hour,
        )

    @classmethod
    def from_runtime(cls, usage: Any) -> UsageBreakdown | None:
        """Read a LiteLLM ``Usage`` object or a plain dict whose ``prompt_tokens`` is total input.

        Lumen's own runtime dict carries the split cache keys directly. A
        LiteLLM usage carries OpenAI-style ``prompt_tokens_details`` (with the
        Anthropic 5m/1h split under ``cache_creation_token_details``) or the
        Anthropic-style top-level counters LiteLLM mirrors onto ``Usage``.
        ``None`` means the payload has no usable prompt/completion totals.
        """
        if usage is None:
            return None
        prompt_tokens = _field(usage, "prompt_tokens")
        completion_tokens = _field(usage, "completion_tokens")
        try:
            prompt_tokens = int(prompt_tokens) if prompt_tokens is not None else None
            completion_tokens = int(completion_tokens) if completion_tokens is not None else None
        except (TypeError, ValueError):
            return None
        if prompt_tokens is None or completion_tokens is None:
            return None
        if isinstance(usage, Mapping) and any(
            key in usage for key in ("cache_creation_5m_input_tokens", "cache_creation_1h_input_tokens")
        ):
            return cls.from_totals(
                prompt_tokens,
                completion_tokens,
                cache_read_input_tokens=usage.get("cache_read_input_tokens"),
                cache_creation_5m_input_tokens=usage.get("cache_creation_5m_input_tokens"),
                cache_creation_1h_input_tokens=usage.get("cache_creation_1h_input_tokens"),
            )
        details = _field(usage, "prompt_tokens_details")
        read = _count(_field(details, "cached_tokens"))
        if read is None:
            read = _count(_field(usage, "cache_read_input_tokens"))
        creation = _count(_field(details, "cache_creation_tokens"))
        if creation is None:
            creation = _count(_field(usage, "cache_creation_input_tokens"))
        if creation is None:
            creation = _count(_field(details, "cache_write_tokens"))
        split = _field(details, "cache_creation_token_details")
        reported_1h = _count(_field(split, "ephemeral_1h_input_tokens"))
        reported_5m = _count(_field(split, "ephemeral_5m_input_tokens"))
        five_minute, one_hour = _split_creation(creation, reported_1h, reported_5m)
        return cls.from_totals(
            prompt_tokens,
            completion_tokens,
            cache_read_input_tokens=read or 0,
            cache_creation_5m_input_tokens=five_minute,
            cache_creation_1h_input_tokens=one_hour,
        )

    @classmethod
    def from_anthropic(cls, usage: object) -> UsageBreakdown | None:
        """Read raw Anthropic Messages usage, where ``input_tokens`` EXCLUDES cache.

        Under native compaction the per-iteration counters are authoritative,
        including their cache counters; the 1h share comes from the top-level
        ``cache_creation`` split, clamped to the summed creation total.
        """
        if not isinstance(usage, Mapping):
            return None
        split = usage.get("cache_creation")
        split = split if isinstance(split, Mapping) else {}
        reported_1h = _count(split.get("ephemeral_1h_input_tokens"))
        reported_5m = _count(split.get("ephemeral_5m_input_tokens"))
        iteration_totals = native_compaction.usage_iteration_breakdown(dict(usage))
        if iteration_totals is not None:
            uncached, output, read, creation = iteration_totals
            five_minute, one_hour = _split_creation(creation, reported_1h, reported_5m)
        else:
            uncached = _count(usage.get("input_tokens"))
            output = _count(usage.get("output_tokens"))
            if uncached is None or output is None:
                return None
            read = _count(usage.get("cache_read_input_tokens")) or 0
            five_minute, one_hour = _split_creation(
                _count(usage.get("cache_creation_input_tokens")), reported_1h, reported_5m
            )
        return cls(
            input_tokens=uncached + read + five_minute + one_hour,
            output_tokens=output,
            cache_read_input_tokens=read,
            cache_creation_5m_input_tokens=five_minute,
            cache_creation_1h_input_tokens=one_hour,
        )

    @classmethod
    def from_responses(cls, usage: object) -> UsageBreakdown | None:
        """Read raw OpenAI Responses usage, where ``input_tokens`` INCLUDES cached tokens."""
        if not isinstance(usage, Mapping):
            return None
        iteration_totals = native_compaction.usage_iteration_totals(dict(usage))
        if iteration_totals is not None:
            input_tokens, output = iteration_totals
        else:
            input_tokens = _count(usage.get("input_tokens"))
            output = _count(usage.get("output_tokens"))
            if input_tokens is None or output is None:
                return None
        details = usage.get("input_tokens_details")
        cached = _count(details.get("cached_tokens")) if isinstance(details, Mapping) else None
        return cls.from_totals(input_tokens, output, cache_read_input_tokens=cached or 0)

    @classmethod
    def from_anthropic_stream(cls, start_usage: object, delta_usages: list[object]) -> UsageBreakdown | None:
        """Merge ``message_start`` usage with cumulative ``message_delta`` usage.

        ``message_delta`` counters are cumulative and may repeat input/cache
        fields, so later non-null fields override earlier ones instead of
        being added.
        """
        merged: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0}
        for part in (start_usage, *delta_usages):
            if isinstance(part, Mapping):
                merged.update({key: value for key, value in part.items() if value is not None})
        return cls.from_anthropic(merged)
