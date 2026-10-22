"""Deterministic local provider catalog, token estimates, and Decimal usage accounting.

Stored manual/models.dev metadata remains authoritative. The bundled catalog is a
frozen historical fallback, not a live provider observation; see data/NOTICE.
"""
from __future__ import annotations

import base64
import copy
import functools
import hashlib
import json
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from importlib.resources import files
from typing import Any, Literal
from urllib.parse import urlsplit

from lumen.services.providers.credentials import api_model_name, transport_model_name
from lumen.services.usage_breakdown import UsageBreakdown, modality_token_charges

logger = logging.getLogger(__name__)

_RAW_COST_QUANTUM = Decimal("0.0000000001")
_TOKENS_PER_MILLION = Decimal("1000000")

# Preserve the historical ledger source; version identifies the frozen fallback.
CATALOG_VERSION = "litellm-1.93.0"
CATALOG_SHA256 = "cc1955f7f1cdc2cdd9ebf216e7eebbc90bbb3528b62439327f7e8b893eda7c12"
CATALOG_PRICE_SOURCE = "litellm"

# Catalog labels classify Vertex publishers/modalities beneath one endpoint.
# These are explicit source-taxonomy aliases, never model-name/family guesses.
_VERTEX_CATALOG_PROVIDERS = frozenset({
    "vertex_ai", "vertex_ai-ai21_models", "vertex_ai-anthropic_models",
    "vertex_ai-deepseek_models", "vertex_ai-embedding-models", "vertex_ai-image-models",
    "vertex_ai-language-models", "vertex_ai-llama_models", "vertex_ai-minimax_models",
    "vertex_ai-mistral_models", "vertex_ai-moonshot_models", "vertex_ai-openai_models",
    "vertex_ai-qwen_models", "vertex_ai-text-models", "vertex_ai-video-models", "vertex_ai-zai_models",
})
_CATALOG_PROVIDER_LABELS = {
    "vertex": _VERTEX_CATALOG_PROVIDERS,
    "vertex_ai": _VERTEX_CATALOG_PROVIDERS,
    "azure": frozenset({"azure", "azure_text"}),
    "bedrock": frozenset({"bedrock", "bedrock_converse"}),
    "cohere": frozenset({"cohere", "cohere_chat"}),
    "together": frozenset({"together_ai"}),
    "together_ai": frozenset({"together_ai"}),
}
_CATALOG_PROVIDER_PREFIXES = {
    "vertex": ("vertex_ai", "vertex"), "vertex_ai": ("vertex_ai", "vertex"),
    "together": ("together_ai", "together"), "together_ai": ("together_ai", "together"),
    "cohere": ("cohere", "cohere_chat"),
}


def _catalog_provider_matches(label: object, provider_type: str) -> bool:
    return label in _CATALOG_PROVIDER_LABELS.get(provider_type, (provider_type,))


@functools.cache
def _catalog() -> dict[str, dict[str, Any]]:
    raw = files("lumen").joinpath("data/provider_catalog.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != CATALOG_SHA256:
        raise ValueError("provider catalog integrity mismatch")
    return json.loads(raw, parse_float=Decimal)


def catalog_model_metadata(model: str, provider_type: str | None = None) -> dict[str, Any]:
    """Detached exact entry; never a model-family/name heuristic."""
    catalog = _catalog()
    for candidate in _pricing_model_candidates(model, provider_type):
        prefixes = _CATALOG_PROVIDER_PREFIXES.get(provider_type, (provider_type,))
        keys = (*(f"{prefix}/{candidate}" for prefix in prefixes), candidate) if provider_type else (candidate,)
        for key in keys:
            entry = catalog.get(key)
            if not isinstance(entry, dict) or key == "sample_spec":
                continue
            if provider_type and not _catalog_provider_matches(entry.get("litellm_provider"), provider_type):
                continue
            return copy.deepcopy(entry)
    return {}


@functools.cache
def catalog_models(provider_type: str) -> tuple[str, ...]:
    """Sorted provider IDs from the frozen catalog, not live discovery."""
    return tuple(sorted(key for key, entry in _catalog().items()
                        if key != "sample_spec" and _catalog_provider_matches(entry.get("litellm_provider"), provider_type)))

# Exact token rates published by Perplexity Agent API that are absent from the
# pinned LiteLLM catalog. This is deliberately not a model-family fallback.
_OFFICIAL_PRICE_PER_MILLION: dict[tuple[str, str], tuple[Decimal, Decimal, str]] = {
    ("perplexity", "perplexity/sonar"): (
        Decimal("0.25"),
        Decimal("2.50"),
        "perplexity_agent_api_2026-09",
    ),
    ("perplexity", "perplexity/glm-5.3"): (
        Decimal("1.40"),
        Decimal("4.40"),
        "perplexity_agent_api_2026-09",
    ),
}

# Custom OpenAI-compatible bases may route the same model ID to a different
# tariff. Only the actual provider endpoint may use bundled provider prices.
_DIRECT_API_BASES = {
    "openai": {"https://api.openai.com", "https://api.openai.com/v1"},
    "anthropic": {"https://api.anthropic.com", "https://api.anthropic.com/v1"},
    "gemini": {"https://generativelanguage.googleapis.com", "https://generativelanguage.googleapis.com/v1beta"},
    "deepseek": {"https://api.deepseek.com", "https://api.deepseek.com/v1"},
    "openrouter": {"https://openrouter.ai/api/v1"},
    "xai": {"https://api.x.ai/v1"},
    "perplexity": {
        "https://api.perplexity.ai", "https://api.perplexity.ai/v1",
        "https://api.perplexity.ai/router", "https://api.perplexity.ai/router/v1",
    },
}


def direct_provider_route(provider_type: str | None, api_base: str | None) -> bool:
    if provider_type not in _DIRECT_API_BASES:
        return False
    return api_base is None or api_base.rstrip("/") in _DIRECT_API_BASES[provider_type]


def bundled_cache_rates(model: str, provider_type: str | None, api_base: str | None) -> dict[str, Decimal | None]:
    """Exact provider catalog rates; a missing cache category remains unpriced."""
    if not direct_provider_route(provider_type, api_base):
        return {}
    metadata = catalog_model_metadata(model, provider_type)
    return {
        key: _as_decimal(metadata.get(field))
        for key, field in (
            ("cache_read_price_per_token", "cache_read_input_token_cost"),
            ("cache_write_price_per_token", "cache_creation_input_token_cost"),
            ("cache_write_1h_price_per_token", "cache_creation_input_token_cost_above_1hr"),
            ("cache_read_price_per_token_above_200k", "cache_read_input_token_cost_above_200k_tokens"),
            ("cache_write_price_per_token_above_200k", "cache_creation_input_token_cost_above_200k_tokens"),
            ("cache_write_1h_price_per_token_above_200k", "cache_creation_input_token_cost_above_1hr_above_200k_tokens"),
        )
    }


@dataclass(frozen=True)
class UsageCost:
    raw_cost: Decimal
    # ``input_cost`` prices UNCACHED input only; cache categories carry their own cost.
    input_cost: Decimal
    output_cost: Decimal
    pricing_status: Literal["priced", "partial", "unpriced"]
    pricing_snapshot: dict[str, object]
    cache_read_cost: Decimal = Decimal("0")
    cache_creation_5m_cost: Decimal = Decimal("0")
    cache_creation_1h_cost: Decimal = Decimal("0")


@dataclass(frozen=True)
class ContextTokenCount:
    """Context accounting result; ``unknown`` is never presented as zero."""

    tokens: int | None
    measurement: Literal["tokenizer", "estimated", "unknown"]
    tokenizer: str | None = None
    reason_code: str | None = None


def count_tokens(model: str, *, messages: list[dict] | None = None, text: str | None = None) -> int:
    """Local text BPE count; legacy integer message counts are estimates."""
    if messages is not None:
        result = count_context_tokens(model, messages)
        if result.tokens is not None:
            return result.tokens
        raw = json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str)
        return max(1, (len(raw) + 3) // 4)
    raw = text or ""
    try:
        return len(_text_encoding(model).encode(raw, disallowed_special=()))
    except (ImportError, OSError, ValueError):
        return max(1, (len(raw) + 3) // 4)


@functools.cache
def _encoding(name: str):
    import tiktoken

    raw = files("lumen").joinpath(f"data/{name}.tiktoken").read_bytes()
    expected = {
        "cl100k_base": "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7",
        "o200k_base": "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d",
    }
    if hashlib.sha256(raw).hexdigest() != expected[name]:
        raise ValueError("tokenizer asset integrity mismatch")
    ranks = {base64.b64decode(token): int(rank) for token, rank in (line.split() for line in raw.splitlines())}
    # Patterns from pinned tiktoken 0.13.0 (MIT); never call its downloading constructors.
    if name == "cl100k_base":
        pattern = r"'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+| ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s"
    else:
        pattern = "|".join((
            r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
            r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
            r"\p{N}{1,3}", r" ?[^\s\p{L}\p{N}]+[\r\n/]*", r"\s*[\r\n]+", r"\s+(?!\S)", r"\s+",
        ))
    return tiktoken.Encoding(name=name, pat_str=pattern, mergeable_ranks=ranks, special_tokens={})


def _text_encoding(model: str):
    import tiktoken.model

    normalized = transport_model_name(model).removeprefix("openai/")
    name = tiktoken.model.MODEL_TO_ENCODING.get(normalized, "cl100k_base")
    return _encoding(name if name in {"cl100k_base", "o200k_base"} else "cl100k_base")


def _contains_uncountable_modality(value: object) -> bool:
    """Reject modalities whose provider representation cannot be measured locally."""
    if isinstance(value, dict):
        kind = value.get("type")
        if kind in {"audio", "audio_url", "input_audio", "video", "input_video", "video_url", "file", "input_file"}:
            return True
        return any(_contains_uncountable_modality(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_uncountable_modality(item) for item in value)
    return False


def _context_text_and_images(value: object) -> tuple[object, int]:
    """Strip image bytes/URLs before BPE; retain the former 250-token estimate."""
    if isinstance(value, dict):
        if value.get("type") in {"image_url", "image", "input_image"}:
            return {"type": "image"}, 1
        result, images = {}, 0
        for key, item in value.items():
            result[key], count = _context_text_and_images(item)
            images += count
        return result, images
    if isinstance(value, (list, tuple)):
        result, images = [], 0
        for item in value:
            part, count = _context_text_and_images(item)
            result.append(part)
            images += count
        return result, images
    return value, 0


def count_context_tokens(model: str, messages: list[dict], tools: list[dict] | None = None) -> ContextTokenCount:
    """Offline context estimate including tools; no provider framing/media claims."""
    tools = tools or []
    if _contains_uncountable_modality(messages) or _contains_uncountable_modality(tools):
        return ContextTokenCount(tokens=None, measurement="unknown", reason_code="token_count_unavailable")
    payload, image_count = _context_text_and_images({"messages": messages, "tools": tools})
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    try:
        encoding = _text_encoding(model)
        return ContextTokenCount(
            tokens=len(encoding.encode(raw, disallowed_special=())) + image_count * 250,
            measurement="estimated", tokenizer=f"tiktoken:0.13.0:{encoding.name}",
            reason_code="image_tokens_estimated" if image_count else "provider_framing_estimated",
        )
    except (ImportError, OSError, ValueError):
        if image_count:
            return ContextTokenCount(tokens=None, measurement="unknown", reason_code="token_count_unavailable")
        return ContextTokenCount(tokens=max(1, (len(raw) + 3) // 4), measurement="estimated",
                                 reason_code="token_counter_failed")


def extract_usage(
    model: str,
    messages: list[dict],
    completion_text: str,
    final_usage: Any | None,
) -> tuple[int, int]:
    """(prompt_tokens, completion_tokens) 산출. prompt_tokens 는 캐시 포함 총 입력이다.

    스트리밍 마지막 청크의 usage 가 있으면 그대로, 없으면 token_counter 폴백.
    이 폴백이 없으면 스트리밍 경로에서 completion_cost 가 0이 되어 과금이 누락된다.
    """
    usage = extract_usage_breakdown(model, messages, completion_text, final_usage)
    return usage.input_tokens, usage.output_tokens


def extract_usage_breakdown(
    model: str,
    messages: list[dict],
    completion_text: str,
    final_usage: Any | None,
) -> UsageBreakdown:
    """Return the ledger breakdown; the token-counter fallback carries no cache split."""
    if final_usage is not None:
        breakdown = UsageBreakdown.from_runtime(final_usage)
        if breakdown is not None:
            return breakdown

    prompt_tokens = count_tokens(model, messages=messages)
    completion_tokens = count_tokens(model, text=completion_text)
    return UsageBreakdown.from_totals(prompt_tokens, completion_tokens)


def _as_decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


def chatgpt_model_metadata(model_name: str) -> dict[str, Any]:
    """Exact catalog entry plus the Codex transport's output-budget limitation."""
    metadata = catalog_model_metadata(model_name, "chatgpt")
    if metadata:
        metadata["output_budget_enforced"] = False
    return metadata


def provider_output_budget_enforced(provider_type: str | None) -> bool | None:
    """Whether the implemented protocol transmits an explicit output budget.

    This is an executor contract, not a model-family capability inference.
    Unknown providers stay unknown, and Codex cannot enforce this parameter.
    """
    if provider_type == "chatgpt":
        return False
    if provider_type in {
        "openai", "anthropic", "gemini", "azure", "azure_ai", "bedrock",
        "vertex", "vertex_ai", "ollama", "openrouter", "deepseek", "xai", "perplexity",
        "mistral", "cohere", "groq", "together", "together_ai",
    }:
        return True
    return None


def _pricing_model_candidates(model: str, provider_type: str | None) -> tuple[str, ...]:
    normalized_model = transport_model_name(model)
    candidates = [normalized_model]
    if provider_type == "perplexity":
        canonical_model = api_model_name(normalized_model, "perplexity")
        if canonical_model not in candidates:
            candidates.append(canonical_model)
    if provider_type and provider_type != "perplexity":
        for prefix in _CATALOG_PROVIDER_PREFIXES.get(provider_type, (provider_type,)):
            if normalized_model.startswith(f"{prefix}/"):
                candidates.append(normalized_model.removeprefix(f"{prefix}/"))
    return tuple(candidates)


def _catalog_component_rates(
    model: str, prompt_tokens: int, completion_tokens: int, provider_type: str | None = None
) -> tuple[Decimal | None, Decimal | None]:
    metadata = catalog_model_metadata(model, provider_type)
    input_rate = _as_decimal(metadata.get("input_cost_per_token"))
    output_rate = _as_decimal(metadata.get("output_cost_per_token"))
    if input_rate is None or output_rate is None or provider_type == "chatgpt" or model.startswith("chatgpt/"):
        return input_rate, output_rate
    thresholds = []
    for key in metadata:
        match = re.fullmatch(r"input_cost_per_token_above_(\d+)(k?)_tokens", key)
        if match:
            threshold = int(match[1]) * (1000 if match[2] else 1)
            if max(1, prompt_tokens) > threshold and _as_decimal(metadata[key]) is not None:
                thresholds.append((threshold, key.removeprefix("input_cost_per_token")))
    if thresholds:
        _, suffix = max(thresholds)
        input_rate = _as_decimal(metadata.get(f"input_cost_per_token{suffix}"))
        tier_output = _as_decimal(metadata.get(f"output_cost_per_token{suffix}"))
        if tier_output is not None:
            output_rate = tier_output
    return input_rate, output_rate


def _official_component_rates(
    model: str, provider_type: str | None, api_base: str | None = None
) -> tuple[Decimal | None, Decimal | None, str | None]:
    if provider_type != "perplexity" or _perplexity_mode(model, api_base) != "agent":
        return None, None, None
    canonical_model = api_model_name(transport_model_name(model), "perplexity")
    published = _OFFICIAL_PRICE_PER_MILLION.get((provider_type, canonical_model))
    if published is None:
        return None, None, None
    input_per_million, output_per_million, source = published
    return input_per_million / _TOKENS_PER_MILLION, output_per_million / _TOKENS_PER_MILLION, source


def _fallback_component_rates(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    provider_type: str | None = None,
    api_base: str | None = None,
) -> tuple[Decimal | None, Decimal | None, str | None]:
    if api_base and not direct_provider_route(provider_type, api_base):
        return None, None, None
    # Agent Sonar is a different priced product from the legacy Sonar API.
    # Exact published Agent rates must win over its legacy bundled alias.
    official_input, official_output, official_source = _official_component_rates(model, provider_type, api_base)
    if official_input is not None and official_output is not None:
        return official_input, official_output, official_source
    if provider_type == "perplexity" and _perplexity_mode(model, api_base) == "legacy":
        # The Responses product has a separate double-prefixed catalog entry.
        # Legacy Sonar must read only its exact Chat API tariff, not that entry.
        model = api_model_name(transport_model_name(model), provider_type).removeprefix("perplexity/")
    input_rate, output_rate = _catalog_component_rates(model, prompt_tokens, completion_tokens, provider_type)
    return input_rate, output_rate, "litellm" if input_rate is not None or output_rate is not None else None


def _component_cost(
    *,
    tokens: int,
    stored_rate: Decimal | None,
    stored_source: str | None,
    fallback_rate: Decimal | None,
    fallback_source: str | None,
) -> tuple[Decimal, Decimal | None, str | None, bool]:
    if stored_rate is not None:
        return stored_rate * tokens, stored_rate, stored_source, True
    if fallback_rate is not None:
        return fallback_rate * tokens, fallback_rate, fallback_source, True
    return Decimal("0"), None, None, False


def effective_prices_per_million(
    model: str, provider_type: str | None = None, *, api_base: str | None = None
) -> tuple[Decimal | None, Decimal | None]:
    """Return exact token prices for the configured API route, never a family alias."""
    input_rate, output_rate, _ = _fallback_component_rates(model, 1_000_000, 1_000_000, provider_type, api_base)

    def display_price(rate: Decimal | None) -> Decimal | None:
        return (
            (rate * _TOKENS_PER_MILLION).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
            if rate is not None
            else None
        )

    return display_price(input_rate), display_price(output_rate)


def official_price_source(model: str, provider_type: str | None = None, *, api_base: str | None = None) -> str | None:
    """Return provenance for an exact documented route price, never a guessed rate."""
    _, _, source = _official_component_rates(model, provider_type, api_base)
    return source


def _decimal_string(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _snapshot_component(
    tokens: int, rate: Decimal | None, cost: Decimal, source: str | None, **extra: object
) -> dict[str, object]:
    return {
        "tokens": tokens,
        "effective_price_per_token": _decimal_string(rate),
        "effective_price_per_million": _decimal_string(rate * _TOKENS_PER_MILLION if rate is not None else None),
        "cost": _decimal_string(cost),
        "source": source,
        **extra,
    }


def cost_from_usage(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    input_price_per_token: Decimal | None,
    output_price_per_token: Decimal | None,
    price_source: str | None,
    provider_type: str | None = None,
    api_base: str | None = None,
    breakdown: UsageBreakdown | None = None,
    cache_read_price_per_token: Decimal | None = None,
    cache_write_price_per_token: Decimal | None = None,
    cache_write_1h_price_per_token: Decimal | None = None,
    cache_read_price_per_token_above_200k: Decimal | None = None,
    cache_write_price_per_token_above_200k: Decimal | None = None,
    cache_write_1h_price_per_token_above_200k: Decimal | None = None,
    cache_price_sources: Mapping[str, str] | None = None,
    allow_catalog_cache: bool = True,
    allow_catalog_prices: bool = True,
    allow_cache_write_fallback: bool = True,
    token_rates: Mapping[str, Any] | None = None,
    required_modalities: Iterable[str] = (),
) -> UsageCost:
    """Bill provider-reported token totals using manual or exact catalog prices.

    Direct-provider catalog rates fill unset categories. A missing 5m write
    inherits the effective input rate, then a missing 1h write inherits the
    effective 5m rate; cache reads still require their own rate.
    Configured ``token_rates`` bill reported image/audio shares at their own
    per-million rates; text prices apply to the residual only.
    """
    if breakdown is None:
        breakdown = UsageBreakdown.from_totals(prompt_tokens, completion_tokens)
    elif (breakdown.input_tokens, breakdown.output_tokens) != (
        max(0, int(prompt_tokens)),
        max(0, int(completion_tokens)),
    ):
        raise ValueError("usage breakdown totals do not match prompt/completion tokens")
    prompt_tokens = breakdown.input_tokens
    completion_tokens = breakdown.output_tokens
    text, modality_charges = modality_token_charges(breakdown, token_rates, required_modalities)
    uncached_tokens = text.uncached_input_tokens
    fallback_input, fallback_output, fallback_source = (
        _fallback_component_rates(model, prompt_tokens, completion_tokens, provider_type, api_base)
        if allow_catalog_prices and (input_price_per_token is None or output_price_per_token is None)
        else (None, None, None)
    )
    input_cost, input_rate, input_source, input_priced = _component_cost(
        tokens=uncached_tokens,
        stored_rate=input_price_per_token,
        stored_source=price_source,
        fallback_rate=fallback_input,
        fallback_source=fallback_source,
    )
    output_cost, output_rate, output_source, output_priced = _component_cost(
        tokens=text.output_tokens,
        stored_rate=output_price_per_token,
        stored_source=price_source,
        fallback_rate=fallback_output,
        fallback_source=fallback_source,
    )
    input_cost = input_cost.quantize(_RAW_COST_QUANTUM, rounding=ROUND_HALF_UP)
    output_cost = output_cost.quantize(_RAW_COST_QUANTUM, rounding=ROUND_HALF_UP)
    catalog_cache = bundled_cache_rates(model, provider_type, api_base) if breakdown.has_cache and allow_catalog_cache else {}
    cache_rates = {
        "cache_read": (
            cache_read_price_per_token, catalog_cache.get("cache_read_price_per_token"),
            cache_read_price_per_token_above_200k, catalog_cache.get("cache_read_price_per_token_above_200k"),
        ),
        "cache_creation_5m": (
            cache_write_price_per_token, catalog_cache.get("cache_write_price_per_token"),
            cache_write_price_per_token_above_200k, catalog_cache.get("cache_write_price_per_token_above_200k"),
        ),
        "cache_creation_1h": (
            cache_write_1h_price_per_token, catalog_cache.get("cache_write_1h_price_per_token"),
            cache_write_1h_price_per_token_above_200k, catalog_cache.get("cache_write_1h_price_per_token_above_200k"),
        ),
    }
    cache_sources: dict[str, str | None] = dict(cache_price_sources or {})
    cache_components: dict[str, tuple[int, Decimal | None, Decimal]] = {}
    for name, tokens in (
        ("cache_read", text.cache_read_input_tokens),
        ("cache_creation_5m", text.cache_creation_5m_input_tokens),
        ("cache_creation_1h", text.cache_creation_1h_input_tokens),
    ):
        configured, catalog, configured_tier, catalog_tier = cache_rates[name]
        rate = configured if configured is not None else catalog
        source = cache_sources.get(name, "manual") if configured is not None else "litellm" if catalog is not None else None
        tier = configured_tier if configured_tier is not None else catalog_tier if source == "litellm" else None
        if prompt_tokens > 200_000 and tier is not None:
            rate = tier
            # A fallback base and its real catalog tier carry separate provenance.
            source = cache_sources.get(f"{name}_above_200k") or source or "litellm"
        if allow_cache_write_fallback and rate is None and name == "cache_creation_5m" and input_rate is not None:
            rate, source = input_rate, f"fallback_input:{input_source or 'unknown'}"
        elif allow_cache_write_fallback and rate is None and name == "cache_creation_1h":
            rate = cache_components["cache_creation_5m"][1]
            source = f"fallback_5m:{cache_sources['cache_creation_5m']}" if rate is not None else None
        cost = (rate * tokens).quantize(_RAW_COST_QUANTUM, rounding=ROUND_HALF_UP) if rate is not None else Decimal("0")
        cache_components[name] = (tokens, rate, cost)
        cache_sources[name] = source
    priced_components = int(input_priced) + int(output_priced)
    pricing_status: Literal["priced", "partial", "unpriced"]
    if priced_components == 2:
        unpriced_cache = any(tokens > 0 and rate is None for tokens, rate, _ in cache_components.values())
        pricing_status = "partial" if unpriced_cache else "priced"
    elif priced_components:
        pricing_status = "partial"
    else:
        pricing_status = "unpriced"
    raw_cost = input_cost + output_cost + sum((cost for _, _, cost in cache_components.values()), Decimal("0"))
    snapshot: dict[str, object] = {
        "model": model,
        "input": _snapshot_component(
            uncached_tokens, input_rate, input_cost, input_source, provider_type=provider_type
        ),
        "output": _snapshot_component(text.output_tokens, output_rate, output_cost, output_source),
    }
    for name, (tokens, rate, cost) in cache_components.items():
        snapshot[name] = _snapshot_component(tokens, rate, cost, cache_sources.get(name))
    cache_read_cost = cache_components["cache_read"][2]
    for charge in modality_charges:
        cost = (charge.price_per_token * charge.tokens).quantize(_RAW_COST_QUANTUM, rounding=ROUND_HALF_UP)
        raw_cost += cost
        if charge.name.endswith("_cache_read"):
            cache_read_cost += cost
        elif charge.name.endswith("_input"):
            input_cost += cost
        else:
            output_cost += cost
        snapshot[charge.name] = _snapshot_component(charge.tokens, charge.price_per_token, cost, "manual")
    return UsageCost(
        raw_cost=raw_cost.quantize(_RAW_COST_QUANTUM, rounding=ROUND_HALF_UP),
        input_cost=input_cost,
        output_cost=output_cost,
        pricing_status=pricing_status,
        pricing_snapshot=snapshot,
        cache_read_cost=cache_read_cost,
        cache_creation_5m_cost=cache_components["cache_creation_5m"][2],
        cache_creation_1h_cost=cache_components["cache_creation_1h"][2],
    )


def _perplexity_mode(model: str, api_base: str | None) -> Literal["router", "agent", "legacy"]:
    base_path = urlsplit(api_base or "").path.rstrip("/").lower()
    if base_path.endswith("/router") or base_path.endswith("/router/v1"):
        return "router"
    if base_path.endswith("/v1"):
        return "agent"
    if model.startswith("perplexity/") and model.count("/") >= 2:
        return "agent"
    if "/" in model and not model.startswith("perplexity/"):
        return "agent"
    return "legacy"

