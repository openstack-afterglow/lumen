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
- ``modality_tokens`` carries provider-REPORTED image/audio shares of those
  aggregates. A direction that is ``None`` was not reported; ``0`` was. A
  modality's ``input_tokens`` includes its own cache read. Cache creation is
  never reported per modality and stays text. Text is the residual.
- Reported shares that exceed their aggregate set ``modality_usage_invalid``;
  the aggregate stays usable for legacy text billing, modality billing must
  fail closed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Any

from lumen.services import native_compaction

CACHE_USAGE_KEYS = (
    "cache_read_input_tokens",
    "cache_creation_5m_input_tokens",
    "cache_creation_1h_input_tokens",
)
MODALITIES = ("image", "audio")
MODALITY_DIRECTIONS = frozenset(f"{modality}_{direction}" for modality in MODALITIES for direction in ("input", "output"))
_MALFORMED = object()

# Evidence for LiteLLM Chat metering only; native adapters are not equivalent.
MODALITY_INPUT_REPORTING_PROVIDERS = {"gemini": frozenset({"image", "audio"})}


def required_modalities_for_request(
    token_rates: Mapping[str, Any],
    *,
    messages: Iterable[Mapping[str, Any]],
    output_modalities: Iterable[str] = (),
) -> list[str]:
    """Priced directions actually requested, not every optional configured price.

    Inspect protocol content parts (including nested tool results), never model
    names, text, or tool schemas. OpenAI Chat, Responses and Anthropic spell
    media parts differently; all carry explicit part types.
    """
    present: set[str] = set()

    def content(value: Any) -> None:
        if isinstance(value, list):
            for part in value:
                content(part)
        elif isinstance(value, Mapping):
            kind = value.get("type")
            if kind in {"image", "image_url", "input_image"}:
                present.add("image_input")
            elif kind in {"audio", "audio_url", "input_audio"}:
                present.add("audio_input")
            if "content" in value:
                content(value["content"])

    for message in messages:
        content(message)
    present.update(f"{name}_output" for name in output_modalities if name in MODALITIES)
    return sorted(
        item for item in present
        if f"{item.rsplit('_', 1)[1]}_per_million" in (token_rates.get(item.rsplit('_', 1)[0]) or {})
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


def _reported(source: Any, key: str) -> object:
    """``None`` when absent, ``_MALFORMED`` when present but not a count."""
    value = _field(source, key)
    if value is None:
        return None
    return _MALFORMED if _count(value) is None else value


def _split_creation(total: int | None, reported_1h: int | None, reported_5m: int | None) -> tuple[int, int]:
    """Return ``(5m, 1h)`` for one cache creation total."""
    if total is None:
        # Only the split was reported; its sum is the creation total.
        return (reported_5m or 0), (reported_1h or 0)
    one_hour = min(reported_1h, total) if reported_1h is not None else 0
    return total - one_hour, one_hour


def _sum_optional(left: int | None, right: int | None) -> int | None:
    if left is None and right is None:
        return None
    return (left or 0) + (right or 0)


@dataclass(frozen=True)
class ModalityTokens:
    """One modality's reported share; ``None`` means the direction was not reported."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        result: dict[str, int] = {}
        if self.input_tokens is not None:
            result["input_tokens"] = self.input_tokens
            result["cache_read_input_tokens"] = self.cache_read_input_tokens
        if self.output_tokens is not None:
            result["output_tokens"] = self.output_tokens
        return result


def _modalities_consistent(base: UsageBreakdown, modalities: Mapping[str, ModalityTokens]) -> bool:
    input_sum = output_sum = cache_sum = 0
    for name, tokens in modalities.items():
        if name not in MODALITIES or not isinstance(tokens, ModalityTokens):
            return False
        for value in (tokens.input_tokens, tokens.output_tokens):
            if value is not None and _count(value) is None:
                return False
        if _count(tokens.cache_read_input_tokens) is None:
            return False
        if tokens.cache_read_input_tokens and (
            tokens.input_tokens is None or tokens.cache_read_input_tokens > tokens.input_tokens
        ):
            return False
        input_sum += tokens.input_tokens or 0
        output_sum += tokens.output_tokens or 0
        cache_sum += tokens.cache_read_input_tokens
    if input_sum > base.input_tokens or output_sum > base.output_tokens or cache_sum > base.cache_read_input_tokens:
        return False
    text_uncached = (
        (base.input_tokens - input_sum) - (base.cache_read_input_tokens - cache_sum) - base.cache_creation_input_tokens
    )
    return text_uncached >= 0


@dataclass(frozen=True)
class UsageBreakdown:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_5m_input_tokens: int = 0
    cache_creation_1h_input_tokens: int = 0
    modality_tokens: Mapping[str, ModalityTokens] = field(default_factory=dict, hash=False)
    modality_usage_invalid: bool = False

    @property
    def cache_creation_input_tokens(self) -> int:
        return self.cache_creation_5m_input_tokens + self.cache_creation_1h_input_tokens

    @property
    def uncached_input_tokens(self) -> int:
        return max(0, self.input_tokens - self.cache_read_input_tokens - self.cache_creation_input_tokens)

    @property
    def has_cache(self) -> bool:
        return bool(self.cache_read_input_tokens or self.cache_creation_input_tokens)

    def reported(self, modality: str, direction: str) -> bool:
        """Whether the provider reported this modality direction (explicit zero included)."""
        tokens = self.modality_tokens.get(modality)
        if tokens is None:
            return False
        return (tokens.input_tokens if direction == "input" else tokens.output_tokens) is not None

    def text_residual(self) -> ModalityTokens:
        """Aggregate minus reported modality shares; cache is removed once."""
        values = self.modality_tokens.values()
        return ModalityTokens(
            input_tokens=max(0, self.input_tokens - sum(item.input_tokens or 0 for item in values)),
            output_tokens=max(0, self.output_tokens - sum(item.output_tokens or 0 for item in values)),
            cache_read_input_tokens=max(
                0, self.cache_read_input_tokens - sum(item.cache_read_input_tokens for item in values)
            ),
        )

    def cache_fields(self) -> dict[str, int]:
        return {
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_5m_input_tokens": self.cache_creation_5m_input_tokens,
            "cache_creation_1h_input_tokens": self.cache_creation_1h_input_tokens,
        }

    def as_usage_dict(self) -> dict[str, Any]:
        """Runtime usage dict: ``prompt_tokens`` stays total input."""
        usage: dict[str, Any] = {
            "prompt_tokens": self.input_tokens,
            "completion_tokens": self.output_tokens,
            **self.cache_fields(),
        }
        modalities = {name: tokens.as_dict() for name, tokens in self.modality_tokens.items() if tokens.as_dict()}
        if modalities:
            usage["modality_tokens"] = modalities
        if self.modality_usage_invalid:
            usage["modality_usage_invalid"] = True
        return usage

    def __add__(self, other: UsageBreakdown) -> UsageBreakdown:
        combined = UsageBreakdown(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
            cache_creation_5m_input_tokens=self.cache_creation_5m_input_tokens + other.cache_creation_5m_input_tokens,
            cache_creation_1h_input_tokens=self.cache_creation_1h_input_tokens + other.cache_creation_1h_input_tokens,
        )
        modalities: dict[str, ModalityTokens] = {}
        for name in MODALITIES:
            left = self.modality_tokens.get(name, ModalityTokens())
            right = other.modality_tokens.get(name, ModalityTokens())

            def direction(left_value: int | None, right_value: int | None, left_total: int, right_total: int):
                # An operand with no tokens in a direction contributes a known zero.
                # Otherwise one unreported round makes the combined share unknown.
                if left_total == 0:
                    return right_value
                if right_total == 0:
                    return left_value
                if left_value is None or right_value is None:
                    return None
                return left_value + right_value

            input_tokens = direction(left.input_tokens, right.input_tokens, self.input_tokens, other.input_tokens)
            output_tokens = direction(left.output_tokens, right.output_tokens, self.output_tokens, other.output_tokens)
            cache = left.cache_read_input_tokens + right.cache_read_input_tokens if input_tokens is not None else 0
            if input_tokens is not None or output_tokens is not None:
                modalities[name] = ModalityTokens(input_tokens, output_tokens, cache)
        return UsageBreakdown.with_modalities(
            combined, modalities, invalid=self.modality_usage_invalid or other.modality_usage_invalid
        )

    @classmethod
    def with_modalities(
        cls, base: UsageBreakdown, modalities: Mapping[str, ModalityTokens], *, invalid: bool = False
    ) -> UsageBreakdown:
        """Attach reported shares; inconsistent shares mark modality usage invalid."""
        kept = {name: tokens for name, tokens in modalities.items()}
        consistent = _modalities_consistent(base, kept)
        return replace(base, modality_tokens=kept, modality_usage_invalid=invalid or not consistent)

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
    def _with_reported(
        cls,
        base: UsageBreakdown,
        inputs: Mapping[str, object],
        outputs: Mapping[str, object],
        caches: Mapping[str, object] | None,
        *,
        zero_is_ambiguous: bool = False,
        invalid: bool = False,
    ) -> UsageBreakdown:
        """Attach raw per-modality counts. ``caches`` ``None`` means no per-modality cache split.

        Without a cache split, a cached aggregate cannot be attributed: a modality
        whose input could contain cached tokens becomes unreported (unknown),
        never silently text. ``zero_is_ambiguous`` covers transports whose
        modality counts already exclude an unsplit cache.
        """
        values = [*inputs.values(), *outputs.values(), *((caches or {}).values())]
        if any(value is _MALFORMED for value in values):
            invalid = True
        modalities: dict[str, ModalityTokens] = {}
        for name in MODALITIES:
            input_tokens = inputs.get(name)
            output_tokens = outputs.get(name)
            input_tokens = None if input_tokens is _MALFORMED else input_tokens
            output_tokens = None if output_tokens is _MALFORMED else output_tokens
            if base.input_tokens == 0:
                input_tokens = 0
            if base.output_tokens == 0:
                output_tokens = 0
            cache = 0
            if caches is not None:
                raw_cache = caches.get(name)
                cache = raw_cache if isinstance(raw_cache, int) and raw_cache is not _MALFORMED else 0
            elif base.cache_read_input_tokens and input_tokens is not None and (input_tokens or zero_is_ambiguous):
                input_tokens = None
            if input_tokens is None:
                if cache:
                    invalid = True
                cache = 0
            if input_tokens is not None or output_tokens is not None:
                modalities[name] = ModalityTokens(input_tokens, output_tokens, cache)
        return cls.with_modalities(base, modalities, invalid=invalid)

    @staticmethod
    def _openai_details(details: Any, *, exhaustive_text: bool = True) -> dict[str, object]:
        """OpenAI ``*_tokens_details``: a provider-emitted ``text_tokens`` makes the split exhaustive.

        LiteLLM fabricates ``text_tokens`` for providers without a modality
        split (Anthropic, Bedrock), so its ``Usage`` objects count only the
        explicitly present image/audio keys.
        """
        if details is None:
            return {}
        counts = {name: _reported(details, f"{name}_tokens") for name in MODALITIES}
        if exhaustive_text and _reported(details, "text_tokens") is not None:
            counts = {name: 0 if value is None else value for name, value in counts.items()}
        return counts

    @classmethod
    def _from_canonical_modalities(cls, base: UsageBreakdown, usage: Mapping, *, strict: bool) -> UsageBreakdown:
        raw = usage.get("modality_tokens")
        invalid = usage.get("modality_usage_invalid") is True
        if raw is None:
            if strict and invalid:
                raise ValueError("modality usage is invalid")
            return replace(base, modality_usage_invalid=invalid)
        modalities: dict[str, ModalityTokens] = {}
        if not isinstance(raw, Mapping) or set(raw) - set(MODALITIES):
            invalid = True
        else:
            for name, item in raw.items():
                if not isinstance(item, Mapping) or set(item) - {"input_tokens", "output_tokens", "cache_read_input_tokens"}:
                    invalid = True
                    continue
                parsed = {key: _reported(item, key) for key in ("input_tokens", "output_tokens", "cache_read_input_tokens")}
                if any(value is _MALFORMED for value in parsed.values()):
                    invalid = True
                    continue
                modalities[name] = ModalityTokens(
                    parsed["input_tokens"], parsed["output_tokens"], parsed["cache_read_input_tokens"] or 0
                )
        result = cls.with_modalities(base, modalities, invalid=invalid)
        if strict and result.modality_usage_invalid:
            raise ValueError("modality usage is invalid")
        return result

    @classmethod
    def from_canonical(cls, usage: object) -> UsageBreakdown:
        """Strictly restore ``as_usage_dict()`` output; any inconsistency raises ``ValueError``."""
        if not isinstance(usage, Mapping):
            raise ValueError("usage must be an object")
        allowed = {"prompt_tokens", "completion_tokens", *CACHE_USAGE_KEYS, "modality_tokens", "modality_usage_invalid"}
        if set(usage) - allowed:
            raise ValueError("usage has unknown fields")
        counts = {key: _count(usage.get(key, 0)) for key in ("prompt_tokens", "completion_tokens", *CACHE_USAGE_KEYS)}
        if "prompt_tokens" not in usage or "completion_tokens" not in usage or None in counts.values():
            raise ValueError("usage totals are invalid")
        base = cls(
            input_tokens=counts["prompt_tokens"],
            output_tokens=counts["completion_tokens"],
            cache_read_input_tokens=counts["cache_read_input_tokens"],
            cache_creation_5m_input_tokens=counts["cache_creation_5m_input_tokens"],
            cache_creation_1h_input_tokens=counts["cache_creation_1h_input_tokens"],
        )
        if base.cache_read_input_tokens + base.cache_creation_input_tokens > base.input_tokens:
            raise ValueError("usage cache exceeds input")
        return cls._from_canonical_modalities(base, usage, strict=True)

    @classmethod
    def from_runtime(cls, usage: Any) -> UsageBreakdown | None:
        """Read a LiteLLM ``Usage`` object or a plain dict whose ``prompt_tokens`` is total input.

        Lumen's own runtime dict carries the split cache keys and modality
        shares directly. A LiteLLM usage carries OpenAI-style
        ``prompt_tokens_details`` (with the Anthropic 5m/1h split under
        ``cache_creation_token_details``) or the Anthropic-style top-level
        counters LiteLLM mirrors onto ``Usage``. LiteLLM's Gemini mapping drops
        the per-modality cache split and subtracts it, so with a cached
        aggregate every modality input becomes unknown.
        ``None`` means the payload has no usable prompt/completion totals.
        """
        if usage is None:
            return None
        prompt_tokens = _field(usage, "prompt_tokens")
        completion_tokens = _field(usage, "completion_tokens")
        invalid = _count(prompt_tokens) is None or _count(completion_tokens) is None
        try:
            prompt_tokens = int(prompt_tokens) if prompt_tokens is not None else None
            completion_tokens = int(completion_tokens) if completion_tokens is not None else None
        except (TypeError, ValueError):
            return None
        if prompt_tokens is None or completion_tokens is None:
            return None
        if isinstance(usage, Mapping) and any(
            key in usage for key in (
                "cache_creation_5m_input_tokens", "cache_creation_1h_input_tokens",
                "modality_tokens", "modality_usage_invalid",
            )
        ):
            base = cls.from_totals(
                prompt_tokens,
                completion_tokens,
                cache_read_input_tokens=usage.get("cache_read_input_tokens"),
                cache_creation_5m_input_tokens=usage.get("cache_creation_5m_input_tokens"),
                cache_creation_1h_input_tokens=usage.get("cache_creation_1h_input_tokens"),
            )
            result = cls._from_canonical_modalities(base, usage, strict=False)
            cache_values = [usage.get(key, 0) for key in CACHE_USAGE_KEYS]
            invalid = invalid or any(_count(value) is None for value in cache_values)
            invalid = invalid or sum(_count(value) or 0 for value in cache_values) > prompt_tokens
            return replace(result, modality_usage_invalid=result.modality_usage_invalid or invalid)
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
        counters = (
            _field(details, "cached_tokens"), _field(usage, "cache_read_input_tokens"),
            _field(details, "cache_creation_tokens"), _field(usage, "cache_creation_input_tokens"),
            _field(details, "cache_write_tokens"),
            _field(split, "ephemeral_1h_input_tokens"), _field(split, "ephemeral_5m_input_tokens"),
        )
        invalid = invalid or any(value is not None and _count(value) is None for value in counters)
        invalid = invalid or (read or 0) + five_minute + one_hour > prompt_tokens
        if creation is not None:
            invalid = invalid or (reported_1h or 0) + (reported_5m or 0) > creation
            if reported_1h is not None and reported_5m is not None:
                invalid = invalid or reported_1h + reported_5m != creation
        base = cls.from_totals(
            prompt_tokens,
            completion_tokens,
            cache_read_input_tokens=read or 0,
            cache_creation_5m_input_tokens=five_minute,
            cache_creation_1h_input_tokens=one_hour,
        )
        cached_split = _field(details, "cached_tokens_details")
        return cls._with_reported(
            base,
            cls._openai_details(details, exhaustive_text=False),
            cls._openai_details(_field(usage, "completion_tokens_details"), exhaustive_text=False),
            cls._openai_details(cached_split, exhaustive_text=False) if cached_split is not None else None,
            zero_is_ambiguous=True,
            invalid=invalid,
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
        base = cls.from_totals(input_tokens, output, cache_read_input_tokens=cached or 0)
        if iteration_totals is not None:
            # Top-level details do not cover the compaction iteration: modality shares are unknown.
            return base
        cached_split = _field(details, "cached_tokens_details")
        return cls._with_reported(
            base,
            cls._openai_details(details),
            cls._openai_details(usage.get("output_tokens_details")),
            cls._openai_details(cached_split) if cached_split is not None else None,
        )

    @classmethod
    def from_openai_media(cls, usage: object) -> UsageBreakdown | None:
        """Read OpenAI Images, token-based transcription, Realtime and Live usage.

        ``input_tokens`` includes cached tokens. Realtime spells the details
        ``input_token_details``/``output_token_details`` and splits the cache
        under ``cached_tokens_details``. Duration-type transcription usage has
        no tokens and returns ``None``.
        """
        if not isinstance(usage, Mapping):
            return None
        input_tokens = _count(usage.get("input_tokens"))
        output = _count(usage.get("output_tokens"))
        if input_tokens is None or output is None:
            return None
        total = _reported(usage, "total_tokens")
        if total is _MALFORMED or (total is not None and total != input_tokens + output):
            return None
        input_details = usage.get("input_tokens_details", usage.get("input_token_details"))
        output_details = usage.get("output_tokens_details", usage.get("output_token_details"))
        cached = _reported(input_details, "cached_tokens")
        base = cls.from_totals(input_tokens, output, cache_read_input_tokens=cached if isinstance(cached, int) else 0)
        cached_split = _field(input_details, "cached_tokens_details")
        return cls._with_reported(
            base,
            cls._openai_details(input_details),
            cls._openai_details(output_details),
            cls._openai_details(cached_split) if cached_split is not None else None,
            invalid=cached is _MALFORMED or (isinstance(cached, int) and cached > input_tokens),
        )

    @staticmethod
    def _google_modalities(entries: object, *, count_keys: tuple[str, ...]) -> dict[str, object] | None:
        """Sum Google ``[{modality, tokenCount}]`` lists; a present list is exhaustive."""
        if entries is None:
            return None
        if not isinstance(entries, list):
            return {name: _MALFORMED for name in MODALITIES}
        counts: dict[str, object] = {name: 0 for name in MODALITIES}
        for entry in entries:
            if not isinstance(entry, Mapping) or not isinstance(entry.get("modality"), str):
                return {name: _MALFORMED for name in MODALITIES}
            raw = next((entry[key] for key in count_keys if key in entry), 0)
            value = _count(raw)
            if value is None:
                return {name: _MALFORMED for name in MODALITIES}
            name = entry["modality"].lower()
            if name in counts:
                counts[name] = int(counts[name]) + value
        return counts

    @staticmethod
    def _merge_optional_counts(*sources: dict[str, object] | None) -> dict[str, object] | None:
        present = [source for source in sources if source is not None]
        if not present:
            return None
        merged: dict[str, object] = {}
        for name in MODALITIES:
            values = [source.get(name) for source in present]
            if any(value is _MALFORMED for value in values):
                merged[name] = _MALFORMED
            else:
                merged[name] = sum(int(value or 0) for value in values)
        return merged

    @classmethod
    def from_gemini(cls, usage_metadata: object) -> UsageBreakdown | None:
        """Read Gemini ``generateContent``/Live ``usageMetadata`` (camelCase or snake_case).

        Input is prompt plus tool-use prompt; output is candidates (Live:
        response) plus thoughts. Proto3 JSON omits zero fields, so an absent
        counter is zero. Modality lists are totals; ``cacheTokensDetails``
        splits the cache.
        """
        if not isinstance(usage_metadata, Mapping):
            return None

        def value(*keys: str) -> object:
            for key in keys:
                if key in usage_metadata:
                    return _reported(usage_metadata, key)
            return None

        prompt = value("promptTokenCount", "prompt_token_count")
        if prompt is None and not any(
            key in usage_metadata
            for key in ("candidatesTokenCount", "candidates_token_count", "responseTokenCount", "response_token_count")
        ):
            return None
        tool = value("toolUsePromptTokenCount", "tool_use_prompt_token_count")
        response = value("responseTokenCount", "response_token_count")
        candidates = value("candidatesTokenCount", "candidates_token_count")
        output = response if response is not None else candidates
        thoughts = value("thoughtsTokenCount", "thoughts_token_count")
        cached = value("cachedContentTokenCount", "cached_content_token_count")
        if any(item is _MALFORMED for item in (prompt, tool, output, thoughts, cached)):
            return None
        input_total = int(prompt or 0) + int(tool or 0)
        output_total = int(output or 0) + int(thoughts or 0)
        if int(cached or 0) > input_total:
            return None
        base = cls.from_totals(input_total, output_total, cache_read_input_tokens=int(cached or 0))
        keys = ("tokenCount", "token_count")

        def details(*names: str) -> dict[str, object] | None:
            for name in names:
                if name in usage_metadata:
                    return cls._google_modalities(usage_metadata[name], count_keys=keys)
            return None

        prompt_details = details("promptTokensDetails", "prompt_tokens_details")
        tool_details = details("toolUsePromptTokensDetails", "tool_use_prompt_tokens_details")
        inputs = cls._merge_optional_counts(prompt_details, tool_details) if prompt_details is not None else None
        if tool and prompt_details is not None and tool_details is None:
            inputs = None
        output_details = (
            details("responseTokensDetails", "response_tokens_details")
            if response is not None
            else details("candidatesTokensDetails", "candidates_tokens_details")
        )
        # Thought tokens are text, so a present candidates list stays exhaustive.
        cache_details = details("cacheTokensDetails", "cache_tokens_details")
        return cls._with_reported(base, inputs or {}, output_details or {}, cache_details)

    @classmethod
    def from_gemini_interactions(cls, usage: object) -> UsageBreakdown | None:
        """Read Gemini Interactions ``usage``.

        Input is ``total_input_tokens`` plus ``total_tool_use_tokens``; output
        is ``total_output_tokens`` plus ``total_thought_tokens`` (thoughts are
        text). ``*_tokens_by_modality`` lists are exhaustive when present.
        """
        if not isinstance(usage, Mapping) or "total_input_tokens" not in usage:
            return None
        counts = {
            key: _reported(usage, key)
            for key in (
                "total_input_tokens",
                "total_output_tokens",
                "total_cached_tokens",
                "total_thought_tokens",
                "total_tool_use_tokens",
                "total_tokens",
            )
        }
        if any(item is _MALFORMED for item in counts.values()):
            return None
        raw_input = int(counts["total_input_tokens"] or 0)
        tool = int(counts["total_tool_use_tokens"] or 0)
        output_total = int(counts["total_output_tokens"] or 0) + int(counts["total_thought_tokens"] or 0)
        total = counts["total_tokens"]
        # Whether tool-use prompt tokens are inside total_input_tokens is only
        # decidable from total_tokens; an undecidable aggregate is unusable.
        if total is not None and total == raw_input + output_total + tool:
            add_tool = bool(tool)
        elif (total is not None and total == raw_input + output_total) or (total is None and not tool):
            add_tool = False
        else:
            return None
        input_total = raw_input + (tool if add_tool else 0)
        cached = int(counts["total_cached_tokens"] or 0)
        if cached > input_total:
            return None
        base = cls.from_totals(input_total, output_total, cache_read_input_tokens=cached)
        keys = ("tokens", "token_count", "tokenCount")

        def details(name: str) -> dict[str, object] | None:
            return cls._google_modalities(usage[name], count_keys=keys) if name in usage else None

        inputs = details("input_tokens_by_modality")
        if add_tool:
            inputs = (
                cls._merge_optional_counts(inputs, details("tool_use_tokens_by_modality"))
                if "tool_use_tokens_by_modality" in usage
                else None
            )
        return cls._with_reported(
            base, inputs or {}, details("output_tokens_by_modality") or {}, details("cached_tokens_by_modality")
        )

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


TOKEN_RATE_FIELDS = ("input_per_million", "cache_read_per_million", "output_per_million")
_TOKENS_PER_MILLION = Decimal("1000000")


@dataclass(frozen=True)
class ModalityCharge:
    """One modality-priced token category; ``name`` is e.g. ``image_cache_read``."""

    name: str
    tokens: int
    price_per_token: Decimal


def _rate(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("token rate is invalid")
    try:
        rate = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("token rate is invalid") from exc
    if not rate.is_finite() or rate < 0:
        raise ValueError("token rate is invalid")
    return rate / _TOKENS_PER_MILLION


def modality_token_charges(
    breakdown: UsageBreakdown,
    token_rates: object,
    required: Iterable[str] = (),
) -> tuple[UsageBreakdown, list[ModalityCharge]]:
    """Split modality-priced tokens off an aggregate and return the text residual.

    A reported direction with a configured modality rate is billed at that
    rate; cached modality tokens need the modality cache rate (else ``ValueError``).
    A direction without a modality rate stays in the text residual exactly as
    aggregate-only billing always did. Invalid reported usage, an unreported
    ``required`` direction or a required direction without a rate raises
    ``ValueError``. Without rates and requirements the breakdown is unchanged.
    """
    if token_rates is None:
        token_rates = {}
    if not isinstance(token_rates, Mapping) or set(token_rates) - set(MODALITIES):
        raise ValueError("token rates are invalid")
    rates: dict[str, dict[str, Decimal]] = {}
    for name, fields in token_rates.items():
        if not isinstance(fields, Mapping) or set(fields) - set(TOKEN_RATE_FIELDS):
            raise ValueError("token rates are invalid")
        rates[name] = {key: _rate(value) for key, value in fields.items()}
    required = tuple(required)
    if any(item not in MODALITY_DIRECTIONS for item in required):
        raise ValueError("unknown required token modality")
    if not any(rates.values()) and not required:
        return breakdown, []
    if breakdown.modality_usage_invalid:
        raise ValueError("provider modality usage is invalid")
    for item in required:
        modality, direction = item.rsplit("_", 1)
        if not breakdown.reported(modality, direction):
            raise ValueError(f"provider did not report {item} tokens")
        if f"{direction}_per_million" not in rates.get(modality, {}):
            raise ValueError(f"{item} token rate is unavailable")
    charges: list[ModalityCharge] = []
    text_input, text_output, text_cache = (
        breakdown.input_tokens, breakdown.output_tokens, breakdown.cache_read_input_tokens,
    )
    for modality in MODALITIES:
        modality_rates = rates.get(modality, {})
        tokens = breakdown.modality_tokens.get(modality, ModalityTokens())
        input_rate = modality_rates.get("input_per_million")
        if input_rate is not None and tokens.input_tokens is not None:
            cached = tokens.cache_read_input_tokens
            cache_rate = modality_rates.get("cache_read_per_million")
            if cached and cache_rate is None:
                raise ValueError(f"{modality} cache read token rate is unavailable")
            charges.append(ModalityCharge(f"{modality}_input", tokens.input_tokens - cached, input_rate))
            charges.append(ModalityCharge(f"{modality}_cache_read", cached, cache_rate or Decimal("0")))
            text_input -= tokens.input_tokens
            text_cache -= cached
        output_rate = modality_rates.get("output_per_million")
        if output_rate is not None and tokens.output_tokens is not None:
            charges.append(ModalityCharge(f"{modality}_output", tokens.output_tokens, output_rate))
            text_output -= tokens.output_tokens
    residual = UsageBreakdown(
        input_tokens=text_input,
        output_tokens=text_output,
        cache_read_input_tokens=text_cache,
        cache_creation_5m_input_tokens=breakdown.cache_creation_5m_input_tokens,
        cache_creation_1h_input_tokens=breakdown.cache_creation_1h_input_tokens,
    )
    if min(text_input, text_output, text_cache, residual.input_tokens - text_cache - residual.cache_creation_input_tokens) < 0:
        raise ValueError("provider modality usage exceeds its aggregate")
    return residual, charges
