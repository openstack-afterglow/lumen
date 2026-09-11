"""Provider price and capability projections."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from lumen.models.chat_db import LlmModel, LlmProvider
from lumen.services.capabilities import (
    apply_subscription_capability_limits,
    litellm_capabilities,
    normalize_capabilities,
)
from lumen.services.litellm_client import effective_prices_per_million

from .billing import billing_capability_for
from .credentials import api_key_source, api_model_name
from .errors import ProviderValidationError

_PER_TOKEN_QUANTUM = Decimal("0.0000000001")
_TOKENS_PER_MILLION = Decimal("1000000")


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
        "billing_capability": billing_capability_for(row.provider_type, auth_mode),
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
) -> tuple[dict, str]:
    """Stored override/models.dev data wins before transport limits are applied."""
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
) -> dict:
    eff_caps, eff_caps_source = _effective_capabilities(row, provider_type, auth_mode)
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
    return {
        "id": row.id,
        "provider_id": row.provider_id,
        "model_name": row.model_name,
        "api_model_name": public_model_name,
        "api_provider": provider_type,
        "display_name": display_name,
        "is_active": row.is_active,
        "is_title_model": row.is_title_model,
        "is_memory_model": row.is_memory_model,
        "input_price_per_million": _per_million_price(row.input_price),
        "output_price_per_million": _per_million_price(row.output_price),
        "effective_input_price_per_million": effective_input_price_per_million,
        "effective_output_price_per_million": effective_output_price_per_million,
        "effective_price_source": effective_price_source,
        "models_dev_model_id": row.models_dev_model_id,
        "price_source": row.price_source,
        "capabilities": row.capabilities,
        "capability_source": row.capability_source,
        "effective_capabilities": eff_caps,
        "effective_capability_source": eff_caps_source,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _pricing_aware_capabilities(
    model: LlmModel,
    capabilities: dict,
    *,
    input_price: Decimal | None = None,
    output_price: Decimal | None = None,
) -> dict:
    metadata = model.price_metadata if isinstance(model.price_metadata, dict) else {}
    metadata = metadata.get("cost", metadata) if isinstance(metadata.get("cost", metadata), dict) else {}

    def has_price(*keys: str) -> bool:
        for key in keys:
            value = metadata.get(key)
            if value is None:
                return False
            try:
                if Decimal(str(value)) < 0:
                    return False
            except (InvalidOperation, ValueError, TypeError):
                return False
        return True

    base_priced = (model.input_price if input_price is None else input_price) is not None and (
        model.output_price if output_price is None else output_price
    ) is not None
    requirements = {
        "text": base_priced,
        "structured_output": base_priced,
        "memory": True,
        "web_search": has_price(
            "web_search_request_per_unit",
            "web_search_context_low_per_unit",
            "web_search_context_medium_per_unit",
            "web_search_context_high_per_unit",
        ),
        "web_fetch": has_price("web_fetch_request_per_unit", "web_fetch_context_per_unit"),
        "image_output": has_price("image_per_unit"),
        "audio_output": has_price("audio_output_per_second"),
        "video_output": has_price("video_per_second"),
        "code_interpreter": has_price("sandbox_per_second"),
        "computer_use": has_price("sandbox_per_second"),
    }
    normalized = dict(capabilities)
    gates = {name: dict(gate) for name, gate in (capabilities.get("feature_gates") or {}).items()}
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
    input_price = Decimal(model.input_price) if model.input_price is not None else None
    output_price = Decimal(model.output_price) if model.output_price is not None else None
    fallback_input, fallback_output = (
        effective_prices_per_million(model.model_name, provider.provider_type)
        if input_price is None or output_price is None
        else (None, None)
    )
    if input_price is None and fallback_input is not None:
        input_price = _per_token_price(fallback_input, "litellm_input_price_per_million")
    if output_price is None and fallback_output is not None:
        output_price = _per_token_price(fallback_output, "litellm_output_price_per_million")
    if input_price is not None and output_price is not None:
        price_source = model.price_source or "litellm"
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
