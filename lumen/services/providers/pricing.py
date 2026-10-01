"""Provider price and capability projections."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation, localcontext

from lumen.models.chat_db import LlmModel, LlmProvider
from lumen.services.capabilities import (
    apply_subscription_capability_limits,
    litellm_capabilities,
    normalize_capabilities,
)
from lumen.services.litellm_client import bundled_cache_rates, effective_prices_per_million, official_price_source

from .billing import billing_capability_for
from .credentials import api_key_source, api_model_name
from .errors import ProviderValidationError

_PER_TOKEN_QUANTUM = Decimal("0.0000000001")
_USD_QUANTUM = Decimal("0.0000000001")
_TOKENS_PER_MILLION = Decimal("1000000")
_MAX_RATE = Decimal("100000000")
MODEL_KINDS = frozenset({"text", "image", "tts", "stt", "realtime"})
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
_DEFAULT_BASIS = {"text": "tokens", "image": "unit", "tts": "duration", "stt": "duration", "realtime": "duration"}
TOKEN_RATE_MODALITIES = ("image", "audio")
TOKEN_RATE_FIELDS = ("input_per_million", "cache_read_per_million", "output_per_million")
MEDIA_PRICE_FIELDS = {
    "text": ("token_rates",),
    "image": ("image_per_unit", "image_variants"),
    "tts": ("audio_per_character",),
    "stt": (),
    "realtime": (),
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


def _validate_token_rates(value) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict) or set(value) - set(TOKEN_RATE_MODALITIES):
        raise ProviderValidationError("token_rates는 image/audio 가격 객체여야 합니다")
    result: dict[str, dict[str, str]] = {}
    for modality, rates in value.items():
        if not isinstance(rates, dict) or not rates or set(rates) - set(TOKEN_RATE_FIELDS):
            raise ProviderValidationError(f"token_rates.{modality} 항목이 올바르지 않습니다")
        if "cache_read_per_million" in rates and "input_per_million" not in rates:
            raise ProviderValidationError(f"token_rates.{modality} 캐시 입력 가격에는 입력 가격이 필요합니다")
        parsed = {key: _strict_decimal(amount, f"token_rates.{modality}.{key}") for key, amount in rates.items()}
        for key, amount in parsed.items():
            if amount and (amount / _TOKENS_PER_MILLION).quantize(_PER_TOKEN_QUANTUM, rounding=ROUND_HALF_UP) == 0:
                raise ProviderValidationError(f"token_rates.{modality}.{key} 값이 저장 정밀도보다 작습니다")
        result[modality] = {key: _decimal_text(amount) for key, amount in parsed.items()}
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


def validate_media_pricing(model_kind: str, pricing: dict | None) -> dict | None:
    """Validate and serialize explicit per-kind USD rates without token-price fallback.

    Absent keys stay absent (unpriced); an explicit zero is a real free rate.
    Requested time units are retained; conversion happens only at calculation.
    """
    if model_kind not in MODEL_KINDS:
        raise ProviderValidationError("지원하지 않는 model_kind 입니다")
    if pricing is None:
        return None
    if not isinstance(pricing, dict):
        raise ProviderValidationError("media_pricing은 객체여야 합니다")
    allowed = set(MEDIA_PRICE_FIELDS[model_kind])
    if set(pricing) - allowed:
        raise ProviderValidationError("model_kind에 맞지 않는 media_pricing 항목입니다")
    result: dict[str, str | dict] = {}
    for key, value in pricing.items():
        if key == "image_variants":
            if not isinstance(value, dict) or len(value) > 500:
                raise ProviderValidationError("image_variants는 variant 가격 객체여야 합니다")
            variants: dict[str, str] = {}
            for variant, amount in value.items():
                if not isinstance(variant, str) or not re.fullmatch(r"(auto|[1-9][0-9]*x[1-9][0-9]*):[A-Za-z0-9][A-Za-z0-9_-]*", variant):
                    raise ProviderValidationError("image_variants는 size:quality 정확한 키를 사용해야 합니다")
                variants[variant] = _decimal_text(_strict_decimal(amount, key))
            result[key] = variants
        elif key == "token_rates":
            result[key] = _validate_token_rates(value)
        elif key == "billing_basis":
            if not isinstance(value, str) or value not in BILLING_BASES.get(model_kind, ()):
                raise ProviderValidationError("model_kind에 맞지 않는 billing_basis 입니다")
            result[key] = value
        else:
            result[key] = _decimal_text(_strict_decimal(value, key))
    for family in _KIND_DURATION_FAMILIES.get(model_kind, ()):
        _check_duration_aliases(result, family)
    basis = result.get("billing_basis")
    if "reservation_usd" in result:
        if basis != "tokens":
            raise ProviderValidationError("reservation_usd는 tokens 과금 기준에서만 사용합니다")
        if Decimal(str(result["reservation_usd"])) <= 0:
            raise ProviderValidationError("reservation_usd는 0보다 커야 합니다")
    elif basis == "tokens":
        raise ProviderValidationError("tokens 과금 기준에는 양수 reservation_usd가 필요합니다")
    return result or None


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
        **{key: text(route.get(key)) for _, _, resolved in CACHE_PRICE_FIELDS
           for key in (resolved, f"{resolved}_above_200k")},
        "cache_price_sources": dict(route.get("cache_price_sources") or {}),
        "price_source": route.get("price_source"),
        "price_version": route.get("price_version"),
        "token_rates": route_token_rates(route),
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


def media_pricing_available(
    kind: str, pricing: dict | None, *, text_input_priced: bool = False, text_output_priced: bool = False
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
            and media_reservation_usd(pricing) is not None
            and all(field in rates.get(modality, {}) for modality, field in needed)
            and all(text_priced[direction] for direction in _TOKEN_BASIS_TEXT_DIRECTIONS[kind])
        )
    if basis == "unit":
        variants = pricing.get("image_variants")
        return bool(variants) if isinstance(variants, dict) else exact_media_price(pricing, "image_per_unit") is not None
    if basis == "characters":
        return exact_media_price(pricing, "audio_per_character") is not None
    if basis == "session":
        return _duration_available(pricing, "realtime_session")
    if basis == "duration":
        return all(_duration_available(pricing, family) for family in _KIND_DURATION_FAMILIES[kind][:2])
    return False


def model_media_pricing_available(model: LlmModel) -> bool:
    """``media_pricing_available`` for a stored media model, including its text columns."""
    return media_pricing_available(
        _kind(model),
        getattr(model, "media_pricing", None),
        text_input_priced=getattr(model, "input_price", None) is not None,
        text_output_priced=getattr(model, "output_price", None) is not None,
    )


def route_media_pricing_available(route: dict) -> bool:
    """``media_pricing_available`` for a resolved route, including its frozen text rates."""
    return media_pricing_available(
        str(route.get("model_kind") or "text"),
        route.get("media_pricing"),
        text_input_priced=route.get("input_price_per_token") is not None,
        text_output_priced=route.get("output_price_per_token") is not None,
    )


def _kind(row: LlmModel) -> str:
    return getattr(row, "model_kind", None) or "text"


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


def _resolved_cache_prices(model: LlmModel, provider: LlmProvider) -> dict[str, Decimal | None]:
    """Manual rates override exact direct-provider catalog rates per category."""
    if _kind(model) != "text":
        # Media models carry only a manual cache-read rate; no catalog fallback.
        prices = {key: None for _, _, resolved in CACHE_PRICE_FIELDS for key in (resolved, f"{resolved}_above_200k")}
        value = getattr(model, "cache_read_price", None)
        if value is not None:
            prices["cache_read_price_per_token"] = Decimal(value).quantize(_PER_TOKEN_QUANTUM, rounding=ROUND_HALF_UP)
        return prices
    catalog = bundled_cache_rates(model.model_name, provider.provider_type, provider.api_base)
    prices: dict[str, Decimal | None] = {}
    for _, column, resolved_key in CACHE_PRICE_FIELDS:
        value = getattr(model, column, None)
        rate = Decimal(value) if value is not None else catalog.get(resolved_key)
        prices[resolved_key] = (
            rate.quantize(_PER_TOKEN_QUANTUM, rounding=ROUND_HALF_UP) if rate is not None else None
        )
        tier_rate = catalog.get(f"{resolved_key}_above_200k") if value is None else None
        prices[f"{resolved_key}_above_200k"] = (
            tier_rate.quantize(_PER_TOKEN_QUANTUM, rounding=ROUND_HALF_UP) if tier_rate is not None else None
        )
    return prices


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
    auth_mode = getattr(row, "auth_mode", "api_key")
    source = api_key_source(row)
    subscription_status = getattr(row, "subscription_status", "disconnected")
    subscription_expires_at = getattr(row, "subscription_expires_at", None)
    encrypted_subscription_tokens = getattr(row, "encrypted_subscription_tokens", None)
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
        "api_base": row.api_base,
        "auth_mode": auth_mode,
        "has_credentials": has_credentials,
        "auth_status": auth_status,
        "auth_expires_at": auth_expires_at,
        "has_api_key": source is not None,
        "api_key_source": source,
        "api_key_env": row.api_key_env if auth_mode == "api_key" else None,
        "has_billing_admin_key": bool(getattr(row, "encrypted_billing_admin_key", None)),
        "billing_capability": billing_capability_for(row.provider_type, auth_mode, row.api_base),
        "is_active": row.is_active,
        "margin_multiplier": float(row.margin_multiplier),
        "models_dev_provider_id": row.models_dev_provider_id,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _effective_capabilities(
    row: LlmModel,
    provider_type: str | None,
    auth_mode: str = "api_key",
    *,
    api_key_configured: bool = False,
    api_base: str | None = None,
) -> tuple[dict, str]:
    """Stored override/models.dev data wins before transport limits are applied."""
    if _kind(row) != "text":
        # Media metadata does not imply an executable route. A real direct
        # provider, configured API key, supported price and asset pipeline matter.
        route_available = False
        kind = _kind(row)
        if kind in {"image", "tts", "stt", "realtime"} and auth_mode == "api_key" and api_key_configured:
            route = {
                "model_kind": kind, "provider_type": provider_type,
                "api_model_name": api_model_name(row.model_name, provider_type or ""),
                "api_base": api_base, "api_key": "configured",
                "media_pricing": getattr(row, "media_pricing", None),
                "input_price_per_token": getattr(row, "input_price", None),
                "output_price_per_token": getattr(row, "output_price", None),
                "cache_read_price_per_token": getattr(row, "cache_read_price", None),
                "provider_auth": None,
            }
            if kind == "image":
                from .image_transport import image_route_ready
                route_available = image_route_ready(route)
            elif kind == "realtime":
                from .realtime_transport import realtime_route_ready
                route_available = realtime_route_ready(route)
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
                        else "route_unavailable"
                    )
                    if name in features
                    else "route_unavailable",
                    "pricing_available": route_available if name in features else False,
                }
                for name in ("text", "image_output", "audio_input", "audio_output")
            }
        }, "registry"
    detected = litellm_capabilities(row.model_name, provider_type)
    if row.capabilities:
        effective = normalize_capabilities(row.capabilities, detected)
        return apply_subscription_capability_limits(effective, auth_mode), (row.capability_source or "override")
    return apply_subscription_capability_limits(detected, auth_mode), "litellm"


def _model_public(
    row: LlmModel,
    *,
    effective_input_price_per_million: Decimal | None = None,
    effective_output_price_per_million: Decimal | None = None,
    effective_price_source: str | None = None,
    provider_type: str | None = None,
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
    if _kind(row) != "text":
        effective_price_source = (
            "manual" if model_media_pricing_available(row) else "unpriced"
        )
    return {
        "id": row.id,
        "provider_id": row.provider_id,
        "model_name": row.model_name,
        "api_model_name": public_model_name,
        "api_provider": provider_type,
        "model_kind": _kind(row),
        "media_pricing": getattr(row, "media_pricing", None),
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
        "models_dev_model_id": row.models_dev_model_id,
        "price_source": row.price_source,
        "capabilities": row.capabilities,
        "capability_source": row.capability_source,
        "effective_capabilities": eff_caps,
        "effective_capability_source": eff_caps_source,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
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
    if _kind(model) != "text":
        normalized = dict(capabilities)
        gates = {name: dict(gate) for name, gate in (capabilities.get("feature_gates") or {}).items()}
        kind = _kind(model)
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
        "memory": True,
        # Native providers report their own search work. Price the selected
        # model's text usage, but never fabricate a managed-search component.
        "web_search": (
            base_priced
            if search_gate.get("mode") == "native"
            else _has_component_prices(
                metadata,
                "web_search_request_per_unit",
                "web_search_context_low_per_unit",
                "web_search_context_medium_per_unit",
                "web_search_context_high_per_unit",
            )
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


def _resolved_base_prices(
    model: LlmModel, provider: LlmProvider
) -> tuple[Decimal | None, Decimal | None, str, str | None]:
    if _kind(model) != "text":
        # Manual text rates only price a token basis; media never borrows catalog prices.
        priced = model_media_pricing_available(model)
        return (
            Decimal(model.input_price) if model.input_price is not None else None,
            Decimal(model.output_price) if model.output_price is not None else None,
            "manual" if priced else "unpriced",
            str(getattr(model, "updated_at", None)) if priced else None,
        )
    input_price = Decimal(model.input_price) if model.input_price is not None else None
    output_price = Decimal(model.output_price) if model.output_price is not None else None
    fallback_input, fallback_output = (
        effective_prices_per_million(model.model_name, provider.provider_type, api_base=provider.api_base)
        if input_price is None or output_price is None
        else (None, None)
    )
    fallback_source = (
        official_price_source(model.model_name, provider.provider_type, api_base=provider.api_base) or "litellm"
    )
    if input_price is None and fallback_input is not None:
        input_price = _per_token_price(fallback_input, "litellm_input_price_per_million")
    if output_price is None and fallback_output is not None:
        output_price = _per_token_price(fallback_output, "litellm_output_price_per_million")
    if input_price is not None and output_price is not None:
        price_source = model.price_source or fallback_source
    elif input_price is not None or output_price is not None:
        price_source = "partial"
    else:
        price_source = "unpriced"
    metadata = model.price_metadata if isinstance(model.price_metadata, dict) else {}
    if price_source == "models.dev":
        price_version = str(metadata.get("fetched_at") or metadata.get("last_updated") or "models.dev")
    elif price_source == "litellm":
        try:
            import litellm

            price_version = str(getattr(litellm, "__version__", "litellm"))
        except Exception:
            price_version = "litellm"
    elif price_source == "manual":
        price_version = str(getattr(model, "updated_at", None) or "manual")
    else:
        price_version = None
    return input_price, output_price, price_source, price_version
