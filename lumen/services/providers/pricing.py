"""Provider price and capability projections."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation, localcontext

from lumen.models.chat_db import LlmModel, LlmProvider
from lumen.services.capabilities import (
    apply_subscription_capability_limits,
    normalize_capabilities,
    provider_capabilities,
)
from lumen.services.native_catalog import MODEL_KINDS
from lumen.services.provider_metadata import (
    bundled_cache_rates,
    catalog_model_metadata,
    catalog_version,
    direct_provider_route,
    effective_prices_per_million,
    native_catalog_entry,
    provider_family,
)

from .billing import billing_capability_for
from .credentials import api_key_source, api_model_name
from .errors import ProviderValidationError

_PER_TOKEN_QUANTUM = Decimal("0.0000000001")
_USD_QUANTUM = Decimal("0.0000000001")
_TOKENS_PER_MILLION = Decimal("1000000")
_MAX_RATE = Decimal("100000000")
_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600}
# Duration family -> (JSON key, seconds per priced unit). Legacy aliases stay accepted.
DURATION_PRICE_FAMILIES: dict[str, tuple[tuple[str, int], ...]] = {
    **{
        family: tuple((f"{family}_per_{unit}", seconds) for unit, seconds in _UNIT_SECONDS.items())
        for family in ("audio_input", "audio_output", "realtime_input", "realtime_output", "realtime_session")
    },
}
DURATION_PRICE_FAMILIES["audio_input"] += (("audio_per_minute", 60),)
DURATION_PRICE_FAMILIES["audio_output"] += (("audio_per_second", 1),)
_KIND_DURATION_FAMILIES = {
    "tts": ("audio_output",),
    "stt": ("audio_input",),
    "realtime": ("realtime_input", "realtime_output", "realtime_session"),
}
BILLING_BASES = {
    "image": frozenset({"unit", "tokens"}),
    "tts": frozenset({"duration", "characters", "tokens"}),
    "stt": frozenset({"duration", "tokens"}),
    "realtime": frozenset({"duration", "session", "tokens"}),
}
# Token-basis kinds whose OpenAI transport derives a request-specific Lumen hold, so an
# explicit reservation_usd is optional there; an operator-supplied value still takes
# precedence. Other providers publish no basis for a derived hold and keep requiring one.
DERIVED_HOLD_TOKEN_KINDS = frozenset({"image", "realtime"})


def derived_hold_allowed(kind: str, provider_type: str | None) -> bool:
    return kind in DERIVED_HOLD_TOKEN_KINDS and provider_type == "openai"

_DEFAULT_BASIS = {
    "text": "tokens", "image": "unit", "tts": "duration", "stt": "duration",
    "realtime": "duration", "embedding": "tokens", "decision": "tokens",
}
TOKEN_RATE_MODALITIES = ("image", "audio")
TOKEN_RATE_FIELDS = ("input_per_million", "cache_read_per_million", "output_per_million")
MEDIA_PRICE_FIELDS = {
    "text": ("token_rates",),
    "image": ("image_per_unit", "image_variants"),
    "tts": ("audio_per_character",),
    "stt": (),
    "realtime": (),
    "embedding": (),
    "decision": (),
}
for _kind_name, _families in _KIND_DURATION_FAMILIES.items():
    MEDIA_PRICE_FIELDS[_kind_name] += tuple(key for family in _families for key, _ in DURATION_PRICE_FAMILIES[family])
for _kind_name in BILLING_BASES:
    MEDIA_PRICE_FIELDS[_kind_name] += ("billing_basis", "reservation_usd", "token_rates")
# Text-price columns a media model may carry for its token basis; cache writes are never reported.
MEDIA_TEXT_PRICE_FIELDS = ("input_price_per_million", "output_price_per_million", "cache_read_price_per_million")


def _strict_decimal(value, field: str) -> Decimal:
    """Exact decimal from a string or integer; floats and bools never become money."""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ProviderValidationError(f"{field} 값은 10진수 문자열이어야 합니다")
    if isinstance(value, str) and not re.fullmatch(r"[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?", value):
        raise ProviderValidationError(f"{field} 값이 올바르지 않습니다")
    try:
        parsed = Decimal(value) if not isinstance(value, Decimal) else value
    except (InvalidOperation, ValueError) as exc:
        raise ProviderValidationError(f"{field} 값이 올바르지 않습니다") from exc
    if not parsed.is_finite() or parsed < 0 or parsed >= _MAX_RATE or parsed.as_tuple().exponent < -18:
        raise ProviderValidationError(f"{field} 값은 유한한 0 이상의 숫자여야 합니다")
    return parsed


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize() if value else Decimal("0"), "f")


def _validate_token_rates(value, *, sparse: bool = False) -> dict[str, dict[str, str | None]]:
    if not isinstance(value, dict) or set(value) - set(TOKEN_RATE_MODALITIES):
        raise ProviderValidationError("token_rates는 image/audio 가격 객체여야 합니다")
    result: dict[str, dict[str, str | None]] = {}
    for modality, rates in value.items():
        if not isinstance(rates, dict) or (not sparse and not rates) or set(rates) - set(TOKEN_RATE_FIELDS):
            raise ProviderValidationError(f"token_rates.{modality} 항목이 올바르지 않습니다")
        if not sparse and "cache_read_per_million" in rates and "input_per_million" not in rates:
            raise ProviderValidationError(f"token_rates.{modality} 캐시 입력 가격에는 입력 가격이 필요합니다")
        parsed = {
            key: None if sparse and amount is None else _strict_decimal(amount, f"token_rates.{modality}.{key}")
            for key, amount in rates.items()
        }
        for key, amount in parsed.items():
            if amount and (amount / _TOKENS_PER_MILLION).quantize(_PER_TOKEN_QUANTUM, rounding=ROUND_HALF_UP) == 0:
                raise ProviderValidationError(f"token_rates.{modality}.{key} 값이 저장 정밀도보다 작습니다")
        result[modality] = {key: _decimal_text(amount) if amount is not None else None for key, amount in parsed.items()}
    return result


def _check_duration_aliases(pricing: dict, family: str) -> tuple[Decimal, int] | None:
    """Return the family's first configured ``(rate, unit_seconds)``; unequal aliases conflict."""
    configured = [
        (_strict_decimal(pricing[key], key), seconds) for key, seconds in DURATION_PRICE_FAMILIES[family] if key in pricing
    ]
    if not configured:
        return None
    rate, seconds = configured[0]
    with localcontext() as context:
        context.prec = 80
        for other_rate, other_seconds in configured[1:]:
            # Cross-multiply: equal per-second prices without lossy division.
            if rate * other_seconds != other_rate * seconds:
                raise ProviderValidationError(f"{family} 시간 단위 가격이 서로 충돌합니다")
    return rate, seconds


def _normalize_media_prices(model_kind: str, pricing: dict | None, *, sparse: bool = False) -> dict | None:
    """Validate price paths and amounts, retaining nulls only for a sparse patch."""
    if model_kind not in MODEL_KINDS:
        raise ProviderValidationError("지원하지 않는 model_kind 입니다")
    if pricing is None:
        return None
    if not isinstance(pricing, dict):
        raise ProviderValidationError("media_pricing은 객체여야 합니다")
    allowed = set(MEDIA_PRICE_FIELDS[model_kind])
    if set(pricing) - allowed:
        raise ProviderValidationError("model_kind에 맞지 않는 media_pricing 항목입니다")
    result = {}
    for key, value in pricing.items():
        if key == "image_variants":
            if not isinstance(value, dict) or len(value) > 500:
                raise ProviderValidationError("image_variants는 variant 가격 객체여야 합니다")
            variants = {}
            for variant, amount in value.items():
                if not isinstance(variant, str) or not re.fullmatch(r"(auto|[1-9][0-9]*x[1-9][0-9]*):[A-Za-z0-9][A-Za-z0-9_-]*", variant):
                    raise ProviderValidationError("image_variants는 size:quality 정확한 키를 사용해야 합니다")
                variants[variant] = None if sparse and amount is None else _decimal_text(_strict_decimal(amount, key))
            result[key] = variants
        elif key == "token_rates":
            result[key] = _validate_token_rates(value, sparse=sparse)
        elif sparse and value is None:
            result[key] = None
        elif key == "billing_basis":
            if not isinstance(value, str) or value not in BILLING_BASES.get(model_kind, ()):
                raise ProviderValidationError("model_kind에 맞지 않는 billing_basis 입니다")
            result[key] = value
        else:
            result[key] = _decimal_text(_strict_decimal(value, key))
    return result or None


def validate_media_pricing(model_kind: str, pricing: dict | None, *, provider_type: str | None = None) -> dict | None:
    """Validate a complete, mode-free media price object."""
    result = _normalize_media_prices(model_kind, pricing)
    if result is None:
        return None
    for family in _KIND_DURATION_FAMILIES.get(model_kind, ()):
        _check_duration_aliases(result, family)
    basis = result.get("billing_basis")
    if "reservation_usd" in result:
        if basis != "tokens":
            raise ProviderValidationError("reservation_usd는 tokens 과금 기준에서만 사용합니다")
        if Decimal(str(result["reservation_usd"])) <= 0:
            raise ProviderValidationError("reservation_usd는 0보다 커야 합니다")
    elif basis == "tokens" and not derived_hold_allowed(model_kind, provider_type):
        raise ProviderValidationError("tokens 과금 기준에는 양수 reservation_usd가 필요합니다")
    return result


def _media_price_overlay(base: dict | None, patch: dict) -> dict | None:
    """Overlay validated price leaves without mutating the base; null removes an override."""
    result = dict(base or {})
    for key, value in patch.items():
        if isinstance(value, dict):
            merged = _media_price_overlay(result.get(key), value)
            if merged:
                result[key] = merged
            else:
                result.pop(key, None)
        elif value is None:
            result.pop(key, None)
        else:
            result[key] = value
    return result or None


def _media_price_values(override: dict) -> dict:
    return {key: value for key, value in override.items() if key != "override_mode"}


def _catalog_media_prices(entry: dict, model_kind: str) -> dict | None:
    return entry.get("media_pricing") if entry.get("kind") == model_kind else None


def validate_media_override(
    model_kind: str, override: dict | None, *, previous: dict | None, model_name: str,
    provider_type: str | None, api_base: str | None,
) -> tuple[dict | None, dict | None]:
    """Return normalized stored override and validated effective pricing for a write."""
    if override is None:
        return None, None
    if not isinstance(override, dict):
        raise ProviderValidationError("media_pricing은 객체여야 합니다")
    mode = override.get("override_mode", (previous or {}).get("override_mode", "replace"))
    if mode not in ("replace", "leaf"):
        raise ProviderValidationError("media_pricing.override_mode는 replace 또는 leaf여야 합니다")
    prices = _media_price_values(override)
    if mode == "replace":
        complete = validate_media_pricing(model_kind, prices, provider_type=provider_type)
        if complete and "override_mode" in override:
            return {"override_mode": "replace", **complete}, complete
        return complete, complete

    patch = _normalize_media_prices(model_kind, prices, sparse=True)
    manual = _media_price_overlay(_media_price_values(previous or {}), patch or {})
    entry = native_catalog_entry(model_name, provider_type, api_base)
    effective = _media_price_overlay(_catalog_media_prices(entry, model_kind), manual or {})
    # Validate the merged object, including limits that span catalog and manual leaves.
    effective = validate_media_pricing(model_kind, effective, provider_type=provider_type)
    return {"override_mode": "leaf", **(manual or {})}, effective


def media_billing_basis(kind: str, pricing: dict | None) -> str:
    """Explicit ``billing_basis`` or the kind's historical unit/duration basis."""
    if kind not in MODEL_KINDS:
        raise ProviderValidationError("지원하지 않는 model_kind 입니다")
    basis = pricing.get("billing_basis") if isinstance(pricing, dict) else None
    if basis is None:
        return _DEFAULT_BASIS[kind]
    if kind == "text" or basis not in BILLING_BASES[kind]:
        raise ProviderValidationError("model_kind에 맞지 않는 billing_basis 입니다")
    return basis


def exact_duration_price(pricing: dict | None, family: str) -> tuple[Decimal, int] | None:
    """``(rate, unit_seconds)`` exactly as configured; conflicting aliases raise."""
    if family not in DURATION_PRICE_FAMILIES:
        raise ProviderValidationError("지원하지 않는 시간 가격 항목입니다")
    if not isinstance(pricing, dict):
        return None
    return _check_duration_aliases(pricing, family)


def duration_cost_usd(pricing: dict | None, family: str, seconds: Decimal | int) -> Decimal | None:
    """Exact USD for elapsed seconds: multiply before dividing, then round once."""
    price = exact_duration_price(pricing, family)
    if price is None:
        return None
    elapsed = Decimal(seconds)
    if not elapsed.is_finite() or elapsed < 0:
        raise ProviderValidationError("시간 사용량이 올바르지 않습니다")
    rate, unit_seconds = price
    with localcontext() as context:
        context.prec = 80
        return (rate * elapsed / unit_seconds).quantize(_USD_QUANTUM, rounding=ROUND_HALF_EVEN)


def media_reservation_usd(pricing: dict | None) -> Decimal | None:
    """The explicit positive token-basis funding envelope, never a derived bound."""
    if not isinstance(pricing, dict) or "reservation_usd" not in pricing:
        return None
    try:
        amount = _strict_decimal(pricing["reservation_usd"], "reservation_usd")
    except ProviderValidationError:
        return None
    return amount if amount > 0 else None


def route_token_rates(route: dict) -> dict[str, dict[str, str]]:
    """Validated per-million image/audio token rates of a resolved route (possibly empty)."""
    pricing = route.get("media_pricing") if isinstance(route, dict) else None
    if not isinstance(pricing, dict) or pricing.get("token_rates") is None:
        return {}
    return _validate_token_rates(pricing["token_rates"])


def frozen_token_pricing(route: dict) -> dict:
    """Admission-time token prices for snapshot billing; missing rates stay ``None``."""

    def text(value) -> str | None:
        return format(Decimal(str(value)), "f") if value is not None else None

    return {
        # Media snapshots never bill an unpriced cache category at zero.
        "model_kind": route.get("model_kind") or "text",
        "input_price_per_token": text(route.get("input_price_per_token")),
        "output_price_per_token": text(route.get("output_price_per_token")),
        "long_context_threshold_tokens": route.get("long_context_threshold_tokens", 200_000),
        "input_price_per_token_above_200k": text(route.get("input_price_per_token_above_200k")),
        "output_price_per_token_above_200k": text(route.get("output_price_per_token_above_200k")),
        **{key: text(route.get(key)) for _, _, resolved in CACHE_PRICE_FIELDS
           for key in (resolved, f"{resolved}_above_200k")},
        "cache_price_sources": dict(route.get("cache_price_sources") or {}),
        "price_source": route.get("price_source"),
        "price_version": route.get("price_version"),
        "token_rates": route_token_rates(route),
        "token_rates_above_200k": _validate_token_rates(route.get("token_rates_above_200k") or {}),
        "token_rates_follow_text": route.get("token_rates_follow_text") is True,
    }


def exact_media_price(pricing: dict | None, field: str, *, variant: str | None = None) -> Decimal | None:
    """Select an image rate: configured variants demand exact size:quality; otherwise universal base."""
    if not isinstance(pricing, dict):
        return None
    variants = pricing.get("image_variants") if field == "image_per_unit" else None
    if isinstance(variants, dict) and variants:
        value = variants.get(variant) if variant is not None else None
    else:
        value = pricing.get(field)
    try:
        price = Decimal(str(value)) if value is not None else None
    except (InvalidOperation, ValueError, TypeError):
        return None
    return price if price is not None and price.is_finite() and price >= 0 else None


def _duration_available(pricing: dict, family: str) -> bool:
    try:
        return exact_duration_price(pricing, family) is not None
    except ProviderValidationError:
        return False


# Text directions every token-basis request consumes: prompts/speech text are
# text input, transcripts text output; image text output may stay unpriced.
_TOKEN_BASIS_TEXT_DIRECTIONS = {
    "image": ("input",),
    "tts": ("input",),
    "stt": ("output",),
    "realtime": ("input", "output"),
}


def _executable_unit_price(value) -> bool:
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return False
    return price.is_finite() and 0 < price < _MAX_RATE and price == price.quantize(_USD_QUANTUM)


def media_pricing_available(
    kind: str, pricing: dict | None, *, text_input_priced: bool = False, text_output_priced: bool = False,
    duration_families: tuple[str, ...] | None = None, provider_type: str | None = None,
) -> bool:
    """Whether the selected billing basis has every rate its execution needs.

    The token basis also needs the model's text rates for the text it always
    consumes; callers pass whether those columns are set.
    """
    if not isinstance(pricing, dict):
        return False
    try:
        basis = media_billing_basis(kind, pricing)
        rates = route_token_rates({"media_pricing": pricing})
    except ProviderValidationError:
        return False
    if basis == "tokens":
        needed = {
            "image": (("image", "output_per_million"),),
            "tts": (("audio", "output_per_million"),),
            "stt": (("audio", "input_per_million"),),
            "realtime": (("audio", "input_per_million"), ("audio", "output_per_million")),
        }.get(kind)
        text_priced = {"input": text_input_priced, "output": text_output_priced}
        return (
            bool(needed)
            and (derived_hold_allowed(kind, provider_type) or media_reservation_usd(pricing) is not None)
            and all(field in rates.get(modality, {}) for modality, field in needed)
            and all(text_priced[direction] for direction in _TOKEN_BASIS_TEXT_DIRECTIONS[kind])
        )
    if basis == "unit":
        # Execution bills only a positive exact size:quality variant; a base rate alone is stored, not executable.
        variants = pricing.get("image_variants")
        return isinstance(variants, dict) and any(_executable_unit_price(value) for value in variants.values())
    if basis == "characters":
        return exact_media_price(pricing, "audio_per_character") is not None
    if basis == "session":
        return _duration_available(pricing, "realtime_session")
    if basis == "duration":
        families = duration_families or _KIND_DURATION_FAMILIES[kind][:2]
        return all(_duration_available(pricing, family) for family in families)
    return False


def _realtime_duration_families(kind: str, model_name: object) -> tuple[str, ...] | None:
    """Input-priced realtime kinds (transcription, translation) settle on fewer duration families."""
    if kind != "realtime":
        return None
    from .realtime_transport import duration_families

    return duration_families(model_name)


def route_media_pricing_available(route: dict) -> bool:
    """``media_pricing_available`` for a resolved route, including its frozen text rates."""
    kind = str(route.get("model_kind") or "text")
    return media_pricing_available(
        kind,
        route.get("media_pricing"),
        text_input_priced=route.get("input_price_per_token") is not None,
        text_output_priced=route.get("output_price_per_token") is not None,
        duration_families=_realtime_duration_families(kind, route.get("api_model_name")),
        provider_type=route.get("provider_type"),
    )


def _kind(row: LlmModel) -> str:
    return getattr(row, "model_kind", None) or "text"


def decision_provider_supported(provider_type: str | None, api_base: str | None, auth_mode: str) -> bool:
    return provider_type == "gemini" and auth_mode == "api_key" and direct_provider_route(provider_type, api_base)


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _to_decimal(value, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise ProviderValidationError(f"{field} 값이 올바르지 않습니다") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ProviderValidationError(f"{field} 값은 유한한 0 이상의 숫자여야 합니다")
    return parsed


def _per_token_price(value, field: str) -> Decimal | None:
    if value is None:
        return None
    per_million = _to_decimal(value, field)
    per_token = (per_million / _TOKENS_PER_MILLION).quantize(_PER_TOKEN_QUANTUM, rounding=ROUND_HALF_UP)
    if per_million != 0 and per_token == 0:
        raise ProviderValidationError(f"{field} 값이 저장 정밀도보다 작습니다")
    return per_token


def _validate_price_pair(input_price_per_million, output_price_per_million) -> tuple[Decimal | None, Decimal | None]:
    if (input_price_per_million is None) != (output_price_per_million is None):
        raise ProviderValidationError("입력·출력 가격은 함께 설정하거나 함께 비워야 합니다")
    return (
        _per_token_price(input_price_per_million, "input_price_per_million"),
        _per_token_price(output_price_per_million, "output_price_per_million"),
    )


def _per_million_price(value: Decimal | None) -> Decimal | None:
    return value * _TOKENS_PER_MILLION if value is not None else None


# (admin per-million field, LlmModel per-token column, resolved per-token key)
CACHE_PRICE_FIELDS = (
    ("cache_read_price_per_million", "cache_read_price", "cache_read_price_per_token"),
    ("cache_write_price_per_million", "cache_write_price", "cache_write_price_per_token"),
    ("cache_write_1h_price_per_million", "cache_write_1h_price", "cache_write_1h_price_per_token"),
)
# Usage/snapshot cache categories, aligned with ``CACHE_PRICE_FIELDS``.
_CACHE_CATEGORIES = ("cache_read", "cache_creation_5m", "cache_creation_1h")


def _resolved_cache_prices(
    model: LlmModel, provider_type: str | None, api_base: str | None
) -> tuple[dict[str, Decimal | None], dict[str, str | None]]:
    """Stated rates: manual (zero included) overrides exact direct-provider catalog per category.

    Only these rates belong to the route identity; inherited writes are derived
    from the separately hashed input price and source.
    """
    prices: dict[str, Decimal | None] = {
        key: None for _, _, resolved in CACHE_PRICE_FIELDS for key in (resolved, f"{resolved}_above_200k")
    }
    sources: dict[str, str | None] = dict.fromkeys(_CACHE_CATEGORIES)
    if _kind(model) == "decision":
        return prices, sources
    catalog = bundled_cache_rates(model.model_name, provider_type, api_base)
    entry = native_catalog_entry(model.model_name, provider_type, api_base) if provider_type == "openai" else {}
    token_pricing = entry.get("token_pricing") or {}
    explicit_write_ttls = token_pricing.get("cache_write_ttl") == "30m" and "long_context" in token_pricing
    for category, (_, column, key) in zip(_CACHE_CATEGORIES, CACHE_PRICE_FIELDS, strict=True):
        manual = getattr(model, column, None)
        rate = Decimal(manual) if manual is not None else catalog.get(key)
        # Long-context tiers are real catalog rates, possibly without a base rate.
        tier = catalog.get(f"{key}_above_200k") if manual is None else None
        prices[key] = rate.quantize(_PER_TOKEN_QUANTUM, rounding=ROUND_HALF_UP) if rate is not None else None
        prices[f"{key}_above_200k"] = (
            tier.quantize(_PER_TOKEN_QUANTUM, rounding=ROUND_HALF_UP) if tier is not None else None
        )
        sources[category] = "manual" if manual is not None else "native_catalog" if rate is not None or tier is not None else None
        if explicit_write_ttls and category != "cache_read" and rate is not None and tier is None and manual is None:
            sources[f"{category}_above_200k"] = "unpublished"
    return prices, sources


def _effective_cache_prices(
    model: LlmModel,
    stated: tuple[dict[str, Decimal | None], dict[str, str | None]],
    input_rate: Decimal | None,
    input_source: str | None = None,
) -> tuple[dict[str, Decimal | None], dict[str, str | None]]:
    """New-admission rates: a missing text 5m write inherits effective input, then 1h inherits 5m.

    Cache reads are never inferred and media models never inherit text writes. A fallback
    fills only the normal-context base: a real catalog >200k tier keeps its own provenance
    under ``<category>_above_200k`` so settlement can name the rate it actually bills.
    """
    prices, sources = dict(stated[0]), dict(stated[1])
    if _kind(model) != "text":
        return prices, sources
    if prices["cache_write_price_per_token"] is None and input_rate is not None:
        if prices["cache_write_price_per_token_above_200k"] is not None:
            sources["cache_creation_5m_above_200k"] = sources["cache_creation_5m"]
        prices["cache_write_price_per_token"] = Decimal(input_rate).quantize(_PER_TOKEN_QUANTUM, rounding=ROUND_HALF_UP)
        sources["cache_creation_5m"] = f"fallback_input:{input_source or 'unknown'}"
    if prices["cache_write_1h_price_per_token"] is None and prices["cache_write_price_per_token"] is not None:
        if prices["cache_write_1h_price_per_token_above_200k"] is not None:
            sources["cache_creation_1h_above_200k"] = sources["cache_creation_1h"]
        elif prices["cache_write_price_per_token_above_200k"] is not None:
            # Only a real catalog 5m tier exists here; manual 5m rates are flat.
            prices["cache_write_1h_price_per_token_above_200k"] = prices["cache_write_price_per_token_above_200k"]
            tier_source = sources.get("cache_creation_5m_above_200k", sources["cache_creation_5m"])
            sources["cache_creation_1h_above_200k"] = f"fallback_5m:{tier_source}"
        prices["cache_write_1h_price_per_token"] = prices["cache_write_price_per_token"]
        sources["cache_creation_1h"] = f"fallback_5m:{sources['cache_creation_5m']}"
    return prices, sources


def _validate_cache_prices(values: dict) -> dict[str, Decimal | None]:
    """Convert present admin per-million cache prices to per-token column values."""
    return {
        column: _per_token_price(values[field], field) for field, column, _ in CACHE_PRICE_FIELDS if field in values
    }


def _json_decimal_strings(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(key): _json_decimal_strings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_decimal_strings(item) for item in value]
    return value


def _provider_public(row: LlmProvider) -> dict:
    """Administrator projection without plaintext or encrypted credential material."""
    from .accounts import default_credential

    credential = default_credential(row)
    auth_mode = getattr(row, "auth_mode", "api_key")
    source = api_key_source(credential) if credential is not None else None
    subscription_status = getattr(credential, "subscription_status", "disconnected")
    subscription_expires_at = getattr(credential, "subscription_expires_at", None)
    encrypted_subscription_tokens = getattr(credential, "encrypted_subscription_tokens", None)
    if auth_mode == "api_key":
        has_credentials = source is not None
        auth_status = "configured" if has_credentials else "disconnected"
        auth_expires_at = None
    else:
        known_expiry_valid = subscription_expires_at is None or (
            subscription_expires_at.replace(tzinfo=UTC)
            if subscription_expires_at.tzinfo is None
            else subscription_expires_at.astimezone(UTC)
        ) > datetime.now(UTC)
        has_credentials = bool(
            encrypted_subscription_tokens
            and subscription_status == "configured"
            and (auth_mode == "chatgpt_device" or known_expiry_valid)
        )
        auth_status = subscription_status
        auth_expires_at = _iso(subscription_expires_at)
        source = None
    return {
        "id": row.id,
        "name": row.name,
        "provider_type": row.provider_type,
        "provider_family": provider_family(row.provider_type, row.api_base, auth_mode),
        "api_provider": row.api_provider,
        "sort_order": row.sort_order,
        "api_base": row.api_base,
        "selection_policy": getattr(row, "selection_policy", "priority"),
        "selected_credential_id": getattr(row, "selected_credential_id", None),
        "auth_mode": auth_mode,
        "has_credentials": has_credentials,
        "auth_status": auth_status,
        "auth_expires_at": auth_expires_at,
        "has_api_key": source is not None,
        "api_key_source": source,
        "api_key_env": getattr(credential, "api_key_env", None) if auth_mode == "api_key" else None,
        "has_billing_admin_key": bool(getattr(row, "encrypted_billing_admin_key", None)),
        "billing_capability": billing_capability_for(row.provider_type, auth_mode, row.api_base),
        "is_active": row.is_active,
        "margin_multiplier": float(row.margin_multiplier),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _media_route(row: LlmModel, provider_type: str | None, api_base: str | None, *, configured: bool) -> dict:
    """Credential-free route projection used to judge media readiness."""
    input_rate, output_rate = _effective_model_rates(row, provider_type, api_base)
    return {
        "model_kind": _kind(row), "provider_type": provider_type,
        "api_model_name": api_model_name(row.model_name, provider_type or ""),
        "api_base": api_base, "api_key": "configured" if configured else None,
        "media_pricing": _effective_media_pricing(row, provider_type, api_base),
        "input_price_per_token": input_rate, "output_price_per_token": output_rate,
        "cache_read_price_per_token": _resolved_cache_prices(row, provider_type, api_base)[0]["cache_read_price_per_token"],
        "provider_auth": None,
    }


def _image_pricing_readiness(row: LlmModel, provider_type: str | None, api_base: str | None) -> dict | None:
    if _kind(row) != "image":
        return None
    from .image_transport import image_pricing_readiness

    return image_pricing_readiness(_media_route(row, provider_type, api_base, configured=True))


def _effective_capabilities(
    row: LlmModel,
    provider_type: str | None,
    auth_mode: str = "api_key",
    *,
    api_key_configured: bool = False,
    api_base: str | None = None,
) -> tuple[dict, str]:
    """Overrides win; legacy imported options/windows fill only missing catalog fields."""
    kind = _kind(row)
    entry = native_catalog_entry(row.model_name, provider_type, api_base) if provider_type == "gemini" else {}
    known_decision = entry.get("kind") == "decision"
    if kind == "decision" or known_decision:
        supported = kind == "decision" and known_decision and decision_provider_supported(provider_type, api_base, auth_mode)
        ready = supported and api_key_configured
        gates = {
            feature: {"available": False, "mode": "none", "reason_code": "route_unavailable", "pricing_available": False}
            for feature in ("text", "embeddings", "image_output", "audio_input", "audio_output")
        }
        gates["decisions"] = {
            "available": ready, "mode": "native" if ready else "none",
            "reason_code": None if ready else "route_unavailable", "pricing_available": False,
        }
        return {
            "decisions": ready,
            "decisions_transport_supported": supported,
            "feature_gates": gates,
            "context_limit": entry["context_window"] if known_decision else None,
            "max_output_tokens": 0,
        }, "native_catalog" if known_decision else "registry"
    if _kind(row) == "embedding":
        ready = auth_mode == "api_key" and api_key_configured and provider_type not in {"anthropic", "chatgpt"}
        gates = {
            name: {"available": False, "mode": "none", "reason_code": "route_unavailable", "pricing_available": False}
            for name in ("text", "image_output", "audio_input", "audio_output")
        }
        gates["embeddings"] = {
            "available": ready, "mode": "native" if ready else "none",
            "reason_code": None if ready else "route_unavailable", "pricing_available": ready,
        }
        stored = row.capabilities if row.capability_source == "override" and row.capabilities else {}
        metadata = (
            catalog_model_metadata(row.model_name, provider_type, api_base)
            if direct_provider_route(provider_type, api_base) else {}
        )
        override = stored.get("embedding")
        embedding = override if isinstance(override, dict) else metadata.get("embedding")
        context_limit = stored.get("context_limit") or metadata.get("max_input_tokens")
        source = "override" if isinstance(override, dict) else "native_catalog" if metadata else "defaults"
        return {"feature_gates": gates, "embedding": embedding, "context_limit": context_limit}, source
    if _kind(row) != "text":
        # Media metadata does not imply an executable route. A real direct
        # provider, configured API key, supported price and asset pipeline matter.
        route_available = False
        reservation_error = False
        pricing_reason = None
        kind = _kind(row)
        configured = auth_mode == "api_key" and api_key_configured
        if kind == "realtime" or (configured and kind in {"image", "tts", "stt"}):
            route = _media_route(row, provider_type, api_base, configured=configured)
            if kind == "image":
                from .image_transport import image_pricing_readiness, image_route_ready
                route_available = image_route_ready(route)
                if configured and not route_available:
                    pricing_reason = image_pricing_readiness(route)["reason"]
            elif kind == "realtime":
                from .realtime_transport import realtime_route_ready
                reservation_error = _realtime_reservation_too_small(route)
                route_available = configured and not reservation_error and realtime_route_ready(route)
            else:
                from .audio_transport import audio_route_ready
                route_available = audio_route_ready(route)
        from lumen.services.assets import asset_pipeline_available

        needs_assets = kind != "realtime"
        ready = route_available and (asset_pipeline_available() if needs_assets else True)
        features = {
            "image": ("image_output",),
            "tts": ("audio_output",),
            "stt": ("audio_input",),
            "realtime": ("audio_input", "audio_output"),
        }.get(kind, ())
        return {
            "feature_gates": {
                name: {
                    "available": ready if name in features else False,
                    "mode": "native" if ready and name in features else "none",
                    "reason_code": (
                        None
                        if ready
                        else "asset_pipeline_unavailable"
                        if route_available and needs_assets
                        else "realtime_reservation_too_small"
                        if reservation_error
                        else pricing_reason
                        if pricing_reason
                        else "route_unavailable"
                    )
                    if name in features
                    else "route_unavailable",
                    "pricing_available": route_available if name in features else False,
                }
                for name in ("text", "image_output", "audio_input", "audio_output")
            }
        }, "registry"
    detected = provider_capabilities(row.model_name, provider_type, api_base)
    legacy = row.capabilities if (
        direct_provider_route(provider_type, api_base)
        and row.capability_source in {"models_dev", "models.dev"}
        and isinstance(row.capabilities, dict)
    ) else {}
    if not detected.get("reasoning_options") and isinstance(legacy.get("reasoning_options"), list):
        detected["reasoning_options"] = legacy["reasoning_options"]
    legacy_limit = legacy.get("context_limit")
    if detected.get("context_limit") is None and type(legacy_limit) is int and legacy_limit > 0:
        detected["context_limit"] = legacy_limit
    if row.capabilities and row.capability_source == "override":
        effective = normalize_capabilities(row.capabilities, detected)
        return apply_subscription_capability_limits(effective, auth_mode), "override"
    source = "native_catalog" if detected.get("metadata_status") == "observed" else "defaults"
    return apply_subscription_capability_limits(detected, auth_mode), source


def domain_billing_readiness(capabilities: dict, pricing: dict) -> dict:
    """Whether a text route can fund a domain-paid call before admission.

    Mirrors ``_provider_credit_bound``: a priced input needs a frozen context
    window and a priced output needs a provider-enforced output budget. Rates
    are the bound's own worst case over text, cache and modality prices, so a
    zero-rate route reserves nothing and stays ready. Unknown windows are never
    fabricated; an administrator may set ``capabilities.context_limit``.
    """
    from lumen.services.credit import worst_case_token_rates

    if pricing.get("input_price_per_token") is None or pricing.get("output_price_per_token") is None:
        return {"ready": False, "reason_code": "pricing_unavailable"}
    try:
        input_rate, output_rate, _ = worst_case_token_rates(pricing)
    except ValueError:
        return {"ready": False, "reason_code": "pricing_unavailable"}
    limit = capabilities.get("context_limit")
    if output_rate > 0 and capabilities.get("output_budget_enforced") is False:
        reason = "output_budget_unenforced"
    elif input_rate > 0 and (type(limit) is not int or limit <= 0):
        reason = "context_limit_unknown"
    else:
        reason = None
    return {"ready": reason is None, "reason_code": reason}


def _model_public(
    row: LlmModel,
    *,
    effective_input_price_per_million: Decimal | None = None,
    effective_output_price_per_million: Decimal | None = None,
    effective_price_source: str | None = None,
    provider_type: str | None = None,
    api_provider: str,
    provider_sort_order: int = 0,
    auth_mode: str = "api_key",
    api_key_configured: bool = False,
    api_base: str | None = None,
) -> dict:
    eff_caps, eff_caps_source = _effective_capabilities(
        row, provider_type, auth_mode, api_key_configured=api_key_configured, api_base=api_base,
    )
    public_model_name = api_model_name(row.model_name, provider_type or "")
    display_name = row.display_name
    if (
        not display_name
        or display_name == row.model_name
        or display_name.startswith("perplexity/perplexity/")
        or display_name.startswith("gemini/gemini-")
        or (provider_type == "gemini" and display_name.startswith("gemini/"))
    ):
        display_name = public_model_name
    resolved_input, resolved_output, resolved_source, _ = _resolved_model_prices(row, provider_type, api_base)
    if effective_input_price_per_million is None:
        effective_input_price_per_million = _per_million_price(resolved_input)
    if effective_output_price_per_million is None:
        effective_output_price_per_million = _per_million_price(resolved_output)
    if effective_price_source is None:
        effective_price_source = resolved_source
    effective_input = (
        effective_input_price_per_million / _TOKENS_PER_MILLION
        if effective_input_price_per_million is not None
        else row.input_price
    )
    effective_output = (
        effective_output_price_per_million / _TOKENS_PER_MILLION
        if effective_output_price_per_million is not None
        else row.output_price
    )
    eff_caps = _pricing_aware_capabilities(
        row,
        eff_caps,
        input_price=effective_input,
        output_price=effective_output,
    )
    entry = native_catalog_entry(row.model_name, provider_type, api_base)
    catalog_entry = None
    if entry:
        catalog_entry = {key: entry[key] for key in ("id", "status", "verified_at", "source_urls")}
        if entry["kind"] == "decision":
            # Identify the supported registry kind even for legacy text rows.
            catalog_entry["kind"] = entry["kind"]
        if entry["kind"] == _kind(row) and _kind(row) in BILLING_BASES:
            # Media editors need the kind and official leaves that a manual override may hide.
            catalog_entry.update(
                kind=entry["kind"], token_pricing=entry.get("token_pricing"), media_pricing=entry.get("media_pricing"),
            )
    price_origin = resolved_source if resolved_source in {"manual", "native_catalog"} else None
    # Stored manual cache columns stay nullable; effective rates are projected separately.
    effective_cache, effective_cache_sources = _effective_cache_prices(
        row, _resolved_cache_prices(row, provider_type, api_base), effective_input, effective_price_source or row.price_source
    )
    stored_media = getattr(row, "media_pricing", None)
    effective_media = _effective_media_pricing(row, provider_type, api_base)
    text_sources = {}
    if effective_input is not None:
        text_sources["input_price_per_million"] = "manual" if row.input_price is not None else "catalog"
    if effective_output is not None and _kind(row) != "embedding":
        text_sources["output_price_per_million"] = "manual" if row.output_price is not None else "catalog"
    for category, (field, _, resolved) in zip(_CACHE_CATEGORIES, CACHE_PRICE_FIELDS, strict=True):
        if effective_cache[resolved] is None:
            continue
        source = effective_cache_sources[category]
        if source == "manual":
            text_sources[field] = "manual"
        elif source == "native_catalog":
            text_sources[field] = "catalog"
        elif source and source.startswith("fallback_input:"):
            text_sources[field] = text_sources["input_price_per_million"]
        elif source and source.startswith("fallback_5m:"):
            text_sources[field] = text_sources["cache_write_price_per_million"]
    domain_billing = None
    if _kind(row) == "text":
        try:
            token_rates = route_token_rates({"media_pricing": effective_media})
        except ProviderValidationError:
            # One malformed stored rate must not break the whole catalog listing.
            domain_billing = {"ready": False, "reason_code": "pricing_unavailable"}
        else:
            domain_billing = domain_billing_readiness(eff_caps, {
                "input_price_per_token": effective_input,
                "output_price_per_token": effective_output,
                **{resolved: effective_cache[resolved] for _, _, resolved in CACHE_PRICE_FIELDS},
                "token_rates": token_rates,
            })
    return {
        "id": row.id,
        "provider_id": row.provider_id,
        "model_name": row.model_name,
        "api_model_name": public_model_name,
        "api_provider": api_provider,
        "provider_type": provider_type,
        "provider_family": provider_family(provider_type, api_base, auth_mode),
        "price_origin": price_origin,
        "catalog_version": catalog_version() if entry else None,
        "catalog_entry": catalog_entry,
        "provider_sort_order": provider_sort_order,
        "sort_order": row.sort_order,
        "model_kind": _kind(row),
        "media_pricing": stored_media,
        "effective_media_pricing": effective_media,
        "effective_media_price_sources": _media_price_sources(effective_media or {}, stored_media or {}),
        "effective_text_price_sources": text_sources,
        "image_pricing_readiness": _image_pricing_readiness(row, provider_type, api_base),
        "display_name": display_name,
        "is_active": row.is_active,
        "is_title_model": row.is_title_model,
        "is_memory_model": row.is_memory_model,
        "input_price_per_million": _per_million_price(row.input_price),
        "output_price_per_million": _per_million_price(row.output_price),
        "effective_input_price_per_million": effective_input_price_per_million,
        "effective_output_price_per_million": effective_output_price_per_million,
        "effective_price_source": effective_price_source,
        **{field: _per_million_price(getattr(row, column, None)) for field, column, _ in CACHE_PRICE_FIELDS},
        **{
            f"effective_{field}": _per_million_price(effective_cache[resolved])
            for field, _, resolved in CACHE_PRICE_FIELDS
        },
        # Normal-context provenance only; >200k tier sources apply per call at settlement.
        "effective_cache_price_sources": {
            category: effective_cache_sources[category] for category in _CACHE_CATEGORIES
        },
        "price_source": row.price_source,
        "capabilities": row.capabilities,
        "capability_source": row.capability_source,
        "effective_capabilities": eff_caps,
        "effective_capability_source": eff_caps_source,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        # Not part of route capabilities, so run config hashes stay unchanged.
        "domain_billing": domain_billing,
    }


def _has_component_prices(metadata: dict, *keys: str) -> bool:
    for key in keys:
        value = metadata.get(key)
        if value is None:
            return False
        try:
            price = Decimal(str(value))
            if not price.is_finite() or price < 0:
                return False
        except (InvalidOperation, ValueError, TypeError):
            return False
    return True


def _pricing_aware_capabilities(
    model: LlmModel,
    capabilities: dict,
    *,
    input_price: Decimal | None = None,
    output_price: Decimal | None = None,
) -> dict:
    kind = _kind(model)
    if kind in {"decision", "embedding"}:
        normalized = dict(capabilities)
        gates = {name: dict(gate) for name, gate in (capabilities.get("feature_gates") or {}).items()}
        gate = gates.get("decisions" if kind == "decision" else "embeddings")
        if gate is not None:
            gate["pricing_available"] = bool(gate.get("available")) and (
                (model.input_price if input_price is None else input_price) is not None
            )
        normalized["feature_gates"] = gates
        return normalized
    if kind != "text":
        normalized = dict(capabilities)
        gates = {name: dict(gate) for name, gate in (capabilities.get("feature_gates") or {}).items()}
        feature = {"image": "image_output", "tts": "audio_output", "stt": "audio_input", "realtime": "audio_input"}[kind]
        available = (gates.get(feature) or {}).get("pricing_available", False)
        for feature in {"image": ("image_output",), "tts": ("audio_output",),
                        "stt": ("audio_input",), "realtime": ("audio_input", "audio_output")}[kind]:
            gates[feature]["pricing_available"] = available
        normalized["feature_gates"] = gates
        return normalized
    metadata = model.price_metadata if isinstance(model.price_metadata, dict) else {}
    metadata = metadata.get("cost", metadata) if isinstance(metadata.get("cost", metadata), dict) else {}

    base_priced = (model.input_price if input_price is None else input_price) is not None and (
        model.output_price if output_price is None else output_price
    ) is not None
    normalized = dict(capabilities)
    gates = {name: dict(gate) for name, gate in (capabilities.get("feature_gates") or {}).items()}
    search_gate = gates.get("web_search") or {}
    requirements = {
        "text": base_priced,
        "structured_output": base_priced,
        "decisions": (model.input_price if input_price is None else input_price) is not None,
        "memory": True,
        # Native providers report their own search work. Price the selected
        # model's text usage, but never fabricate a managed-search component.
        "web_search": (
            _has_component_prices(
                metadata,
                "web_search_request_per_unit",
                "web_search_context_low_per_unit",
                "web_search_context_medium_per_unit",
                "web_search_context_high_per_unit",
            )
            if search_gate.get("mode") == "managed" else False
        ),
        "web_fetch": _has_component_prices(metadata, "web_fetch_request_per_unit", "web_fetch_context_per_unit"),
        "image_output": _has_component_prices(metadata, "image_per_unit"),
        "audio_output": _has_component_prices(metadata, "audio_output_per_second"),
        "video_output": _has_component_prices(metadata, "video_per_second"),
        "code_interpreter": _has_component_prices(metadata, "sandbox_per_second"),
        "computer_use": _has_component_prices(metadata, "sandbox_per_second"),
    }
    for feature, gate in gates.items():
        if feature in requirements:
            gate["pricing_available"] = requirements[feature]
    if search_gate.get("mode") == "native":
        gates["web_search"].update(
            available=False, mode="none", reason_code="pricing_unavailable", pricing_available=False,
        )
    from lumen.services.assets import asset_pipeline_available

    for feature in ("image_input", "document_input"):
        gate = gates.get(feature)
        if isinstance(gate, dict) and gate.get("available") and not asset_pipeline_available():
            gate.update(
                available=False,
                mode="none",
                reason_code="asset_pipeline_unavailable",
                pricing_available=False,
            )
    normalized["feature_gates"] = gates
    from lumen.config import get_settings
    from lumen.services.sandbox_runtime import sandbox_available

    if not sandbox_available(get_settings()):
        for feature in ("code_interpreter", "computer_use"):
            gate = gates.get(feature)
            if isinstance(gate, dict) and gate.get("available"):
                gate.update(
                    available=False,
                    mode="none",
                    reason_code="sandbox_unavailable",
                    pricing_available=False,
                )
    return normalized


def _effective_model_rates(
    model: LlmModel, provider_type: str | None, api_base: str | None,
) -> tuple[Decimal | None, Decimal | None]:
    if _kind(model) == "decision":
        if model.input_price is not None:
            return Decimal(model.input_price), None
        entry = native_catalog_entry(model.model_name, provider_type, api_base)
        if entry.get("kind") == "decision":
            return Decimal(entry["token_pricing"]["input"]) / _TOKENS_PER_MILLION, None
        return None, None
    fallback_input, fallback_output = effective_prices_per_million(model.model_name, provider_type, api_base=api_base)
    if (
        provider_type == "gemini" and fallback_input is not None and fallback_output is None
        and native_catalog_entry(model.model_name, provider_type, api_base).get("kind") == "decision"
    ):
        fallback_input = None
    return (
        Decimal(model.input_price) if model.input_price is not None else fallback_input / _TOKENS_PER_MILLION if fallback_input is not None else None,
        Decimal(model.output_price) if model.output_price is not None else fallback_output / _TOKENS_PER_MILLION if fallback_output is not None else None,
    )




def _media_price_sources(effective: dict, stored: dict) -> dict:
    sources = {}
    for key, value in effective.items():
        if isinstance(value, dict):
            nested = _media_price_sources(value, stored.get(key) or {})
            if nested:
                sources[key] = nested
        else:
            sources[key] = "manual" if key in stored else "catalog"
    return sources


def _has_manual_media_prices(pricing: dict | None) -> bool:
    # Existing mode-free objects retain their historical provenance.
    return pricing is not None and (pricing.get("override_mode") != "leaf" or len(pricing) > 1)


def _effective_media_pricing(model: LlmModel, provider_type: str | None, api_base: str | None) -> dict | None:
    kind = _kind(model)
    if kind == "decision":
        return None
    manual = getattr(model, "media_pricing", None)
    if manual is not None and manual.get("override_mode") != "leaf":
        return _media_price_values(manual) if "override_mode" in manual else manual
    entry = native_catalog_entry(model.model_name, provider_type, api_base)
    base = _catalog_media_prices(entry, kind)
    return _media_price_overlay(base, _media_price_values(manual)) if manual and len(manual) > 1 else base


def _resolved_context_tiers(model: LlmModel, provider_type: str | None, api_base: str | None) -> dict:
    entry = {} if _kind(model) == "decision" else native_catalog_entry(model.model_name, provider_type, api_base)
    token = entry.get("token_pricing") or {}
    tier = token.get("long_context") or {}
    manual_media = getattr(model, "media_pricing", None)
    leaf_mode = (manual_media or {}).get("override_mode") == "leaf"
    catalog_media = (manual_media is None or leaf_mode) and entry.get("kind") == _kind(model)
    manual_rates = (manual_media or {}).get("token_rates", {}) if leaf_mode else {}
    catalog_text = all(getattr(model, field, None) is None for field in ("input_price", "output_price", "cache_read_price"))
    follow_text = (
        bool(tier) and catalog_media and catalog_text and not manual_rates
        and bool((entry.get("media_pricing") or {}).get("token_rates"))
    )
    media = entry.get("media_pricing") if catalog_media else None
    tier_rates = tier.get("token_rates") or {}
    input_rate, output_rate = _effective_model_rates(model, provider_type, api_base)
    cache_read = getattr(model, "cache_read_price", None)
    if cache_read is None and token.get("cache_read") is not None:
        cache_read = Decimal(token["cache_read"]) / _TOKENS_PER_MILLION
    effective_text = {
        "input_per_million": input_rate * _TOKENS_PER_MILLION if input_rate is not None else None,
        "output_per_million": output_rate * _TOKENS_PER_MILLION if output_rate is not None else None,
        "cache_read_per_million": Decimal(cache_read) * _TOKENS_PER_MILLION if cache_read is not None else None,
    }
    resolved_modality_tiers = {}
    for modality, fields in (((media or {}).get("token_rates") or {}) if tier else {}).items():
        same_base = all(
            effective_text.get(field) is not None and Decimal(value) == effective_text[field]
            for field, value in fields.items()
        )
        same_tier = all(
            Decimal(value) == Decimal(tier.get(field.removesuffix("_per_million"), token[field.removesuffix("_per_million")]))
            for field, value in tier_rates.get(modality, {}).items()
        ) if same_base else False
        if follow_text and same_base and same_tier:
            continue
        manual_fields = manual_rates.get(modality, {})
        if not fields.keys() - manual_fields.keys():
            continue
        resolved_modality_tiers[modality] = {
            field: manual_fields[field] if field in manual_fields else tier_rates.get(modality, {}).get(
                field, tier.get(field.removesuffix("_per_million"), token.get(field.removesuffix("_per_million"))),
            )
            for field in fields
        }
    return {
        "long_context_threshold_tokens": tier.get("threshold_tokens", 200_000),
        "token_rates_above_200k": resolved_modality_tiers,
        "token_rates_follow_text": follow_text,
        **{
            f"{direction}_price_per_token_above_200k": (
                Decimal(tier[direction]) / _TOKENS_PER_MILLION
                if getattr(model, f"{direction}_price", None) is None and tier.get(direction) is not None else None
            )
            for direction in ("input", "output")
        },
    }


def _resolved_base_prices(
    model: LlmModel, provider: LlmProvider,
) -> tuple[Decimal | None, Decimal | None, str, str | None]:
    return _resolved_model_prices(model, provider.provider_type, provider.api_base)


def _realtime_reservation_too_small(route: dict) -> bool:
    from .realtime_transport import RealtimeReservationTooSmall, validate_configured_reservation

    try:
        validate_configured_reservation(route)
    except RealtimeReservationTooSmall:
        return True
    except ProviderValidationError:
        pass  # Other invalid pricing is reported by the normal readiness checks.
    return False


def _resolved_model_prices(
    model: LlmModel, provider_type: str | None, api_base: str | None,
) -> tuple[Decimal | None, Decimal | None, str, str | None]:
    input_price, output_price = _effective_model_rates(model, provider_type, api_base)
    kind = _kind(model)
    if kind == "embedding":
        output_price = Decimal(0)
    priced = input_price is not None and (kind == "decision" or output_price is not None)
    manual = model.input_price is not None
    if kind != "decision":
        manual = manual or model.output_price is not None or _has_manual_media_prices(getattr(model, "media_pricing", None))
    if kind not in {"text", "embedding", "decision"}:
        media = _effective_media_pricing(model, provider_type, api_base)
        priced = media_pricing_available(
            kind, media, text_input_priced=input_price is not None, text_output_priced=output_price is not None,
            duration_families=_realtime_duration_families(kind, model.model_name), provider_type=provider_type,
        )
        if priced and kind == "image":
            # Priced variants the model cannot execute do not make the row priced.
            readiness = _image_pricing_readiness(model, provider_type, api_base)
            priced = readiness["reason"] != "unsupported_image_variants"
        if priced and kind == "realtime":
            cache_rates, _ = _resolved_cache_prices(model, provider_type, api_base)
            priced = not _realtime_reservation_too_small({"model_kind": kind, "provider_type": provider_type,
                "media_pricing": media, "input_price_per_token": input_price, "output_price_per_token": output_price,
                "cache_read_price_per_token": cache_rates["cache_read_price_per_token"]})
    if priced:
        source = "manual" if manual else "native_catalog"
    else:
        source = "partial" if input_price is not None or output_price is not None else "unpriced"
    version = str(getattr(model, "updated_at", None) or "manual") if source == "manual" else catalog_version() if source == "native_catalog" else None
    return input_price, output_price, source, version
