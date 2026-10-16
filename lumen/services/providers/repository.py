"""Provider and model administration repository."""

from __future__ import annotations

import re

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from lumen.crypto import encrypt_llm_provider_billing_admin_key, encrypt_llm_provider_key
from lumen.db import mark_db_unhealthy
from lumen.models.chat_db import LlmModel, LlmProvider
from lumen.services.litellm_client import direct_provider_route
from lumen.services.models_dev import ModelsDevCatalog

from .billing import billing_admin_key_supported, billing_capability_for
from .credentials import (
    api_key_source,
    api_model_name,
    canonical_subscription_model_name,
    normalize_api_key_env,
    perplexity_route_model_name,
)
from .errors import (
    ChatStorageUnavailable,
    ModelOrderConflictError,
    ModelsDevImportConflictError,
    ProviderNotFoundError,
    ProviderValidationError,
)
from .pricing import (
    MODEL_KINDS,
    _json_decimal_strings,
    _model_public,
    _per_million_price,
    _per_token_price,
    _provider_public,
    _resolved_base_prices,
    _to_decimal,
    _validate_cache_prices,
    validate_media_pricing,
)
from .routing import _lock_mutable_route, _require_db

_AUTH_MODES = frozenset({"api_key", "chatgpt_device", "anthropic_subscription"})
_MEDIA_PROVIDERS = frozenset({"openai", "gemini"})
_DEFAULT_API_PROVIDER = object()


def validate_api_provider(value: str) -> str:
    if not isinstance(value, str):
        raise ProviderValidationError("api_provider 은 유효한 식별자여야 합니다")
    normalized = value.strip().lower()
    if len(normalized) > 40 or not re.fullmatch(r"[a-z][a-z0-9_-]*", normalized):
        raise ProviderValidationError("api_provider 은 40자 이하 영문 식별자여야 합니다")
    return normalized


def validate_sort_order(value: int) -> int:
    if type(value) is not int or not 0 <= value <= 2147483647:
        raise ProviderValidationError("sort_order 은 0..2147483647 범위의 정수여야 합니다")
    return value


def validate_model_order(value: list[int]) -> list[int]:
    if not isinstance(value, list) or not 1 <= len(value) <= 500:
        raise ProviderValidationError("모델 ID 목록은 1개 이상 500개 이하여야 합니다")
    if any(type(model_id) is not int or not 1 <= model_id <= 9223372036854775807 for model_id in value):
        raise ProviderValidationError("모델 ID는 양의 정수여야 합니다")
    if len(set(value)) != len(value):
        raise ProviderValidationError("모델 ID 목록에는 중복이 없어야 합니다")
    return value


def validate_provider_name(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 100:
        raise ProviderValidationError("name 은 100자 이하의 비어 있지 않은 문자열이어야 합니다")
    return value.strip()


def _validate_model_kind(provider: LlmProvider, kind: str) -> None:
    if kind not in MODEL_KINDS:
        raise ProviderValidationError("지원하지 않는 model_kind 입니다")
    if kind != "text" and (provider.provider_type not in _MEDIA_PROVIDERS or provider.auth_mode != "api_key" or not direct_provider_route(provider.provider_type, provider.api_base)):
        raise ProviderValidationError("media 모델은 지원되는 direct API-key 프로바이더에서만 등록할 수 있습니다")


def _validate_kind_prices(kind: str, media_pricing: dict | None, token_prices: tuple) -> dict | None:
    """Media models may carry text input/output/cache-read rates for a token basis, never cache writes."""
    if kind != "text" and any(value is not None for value in token_prices[3:]):
        raise ProviderValidationError("media 모델에는 cache write 가격을 설정할 수 없습니다")
    return validate_media_pricing(kind, media_pricing)


def _validate_text_prices(input_price_per_million, output_price_per_million):
    """Store each text direction independently, including legacy text-kind media models."""
    return (
        _per_token_price(input_price_per_million, "input_price_per_million"),
        _per_token_price(output_price_per_million, "output_price_per_million"),
    )


def validate_provider_auth_configuration(
    *,
    provider_type: str,
    auth_mode: str,
    api_base: str | None,
    api_key: str | None,
    api_key_env: str | None,
    billing_admin_key: str | None = None,
) -> None:
    if auth_mode not in _AUTH_MODES:
        raise ProviderValidationError("지원하지 않는 provider auth_mode 입니다")
    if provider_type == "chatgpt" and auth_mode != "chatgpt_device":
        raise ProviderValidationError("ChatGPT 프로바이더는 구독 device 인증만 지원합니다")
    if auth_mode == "chatgpt_device" and provider_type != "chatgpt":
        raise ProviderValidationError("ChatGPT 구독은 provider_type=chatgpt 이어야 합니다")
    if auth_mode == "anthropic_subscription" and provider_type != "anthropic":
        raise ProviderValidationError("Claude 구독은 provider_type=anthropic 이어야 합니다")
    if auth_mode != "api_key" and any(
        isinstance(value, str) and value.strip() for value in (api_base, api_key, api_key_env)
    ):
        raise ProviderValidationError("구독 프로바이더에는 API base/key/environment credential을 설정할 수 없습니다")
    if billing_admin_key and not billing_admin_key_supported(provider_type, auth_mode, api_base):
        raise ProviderValidationError("이 프로바이더에는 관리자 결제 키를 설정할 수 없습니다")


async def _lock_subscription_namespaces(session) -> dict[int, LlmProvider]:
    providers = (
        await session.execute(
            select(LlmProvider).where(LlmProvider.auth_mode != "api_key").order_by(LlmProvider.id).with_for_update()
        )
    ).scalars()
    return {provider.id: provider for provider in providers}


async def _reject_duplicate_subscription_model(
    session,
    *,
    namespace_provider_ids: tuple[int, ...],
    model_name: str,
    current_model_id: int | None = None,
) -> None:
    if not namespace_provider_ids:
        return
    stmt = select(LlmModel.id).where(
        LlmModel.provider_id.in_(namespace_provider_ids),
        LlmModel.model_name == model_name,
    )
    if current_model_id is not None:
        stmt = stmt.where(LlmModel.id != current_model_id)
    if (await session.execute(stmt.limit(1))).scalar_one_or_none() is not None:
        raise ProviderValidationError("같은 구독 model_name 은 하나의 프로바이더에만 등록할 수 있습니다")


async def _reject_duplicate_perplexity_model(
    session,
    *,
    provider_id: int,
    api_name: str,
    current_model_id: int | None = None,
) -> None:
    stmt = select(LlmModel).where(LlmModel.provider_id == provider_id).order_by(LlmModel.id)
    if current_model_id is not None:
        stmt = stmt.where(LlmModel.id != current_model_id)
    rows = (await session.execute(stmt)).scalars().all()
    if any(api_model_name(row.model_name, "perplexity") == api_name for row in rows):
        raise ProviderValidationError("프로바이더 내 공개 model_name 이 중복됩니다")


async def create_provider(
    *,
    name: str,
    provider_type: str = "openai",
    api_provider: str = _DEFAULT_API_PROVIDER,
    sort_order: int = 0,
    api_base: str | None = None,
    api_key: str | None = None,
    api_key_env: str | None = None,
    auth_mode: str = "api_key",
    billing_admin_key: str | None = None,
    margin_multiplier=1.0,
    models_dev_provider_id: str | None = None,
    is_active: bool = True,
) -> dict:
    factory = _require_db()
    name = validate_provider_name(name)
    sort_order = validate_sort_order(sort_order)
    normalized_provider_type = (provider_type or "openai").strip()
    api_provider = validate_api_provider(
        normalized_provider_type if api_provider is _DEFAULT_API_PROVIDER else api_provider
    )
    normalized_auth_mode = (auth_mode or "api_key").strip()
    validate_provider_auth_configuration(
        provider_type=normalized_provider_type,
        auth_mode=normalized_auth_mode,
        api_base=api_base,
        api_key=api_key,
        api_key_env=api_key_env,
        billing_admin_key=billing_admin_key,
    )
    row = LlmProvider(
        name=name.strip(),
        provider_type=normalized_provider_type,
        api_provider=api_provider,
        sort_order=sort_order,
        api_base=(api_base or None) if normalized_auth_mode == "api_key" else None,
        encrypted_api_key=(
            encrypt_llm_provider_key(api_key) if api_key and normalized_auth_mode == "api_key" else None
        ),
        encrypted_billing_admin_key=(
            encrypt_llm_provider_billing_admin_key(billing_admin_key)
            if billing_admin_key
            and billing_admin_key_supported(normalized_provider_type, normalized_auth_mode, api_base)
            else None
        ),
        api_key_env=normalize_api_key_env(api_key_env) if normalized_auth_mode == "api_key" else None,
        auth_mode=normalized_auth_mode,
        margin_multiplier=_to_decimal(margin_multiplier, "margin_multiplier"),
        models_dev_provider_id=(models_dev_provider_id or None),
        is_active=is_active,
    )
    try:
        async with factory() as session, session.begin():
            session.add(row)
            await session.flush()
            return _provider_public(row)
    except IntegrityError as exc:
        raise ProviderValidationError(f"이미 존재하는 프로바이더 이름입니다: {name}") from exc
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def list_providers() -> list[dict]:
    factory = _require_db()
    try:
        async with factory() as session:
            rows = (await session.execute(
                select(LlmProvider).order_by(LlmProvider.sort_order, LlmProvider.id)
            )).scalars().all()
            return [_provider_public(row) for row in rows]
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def update_provider(provider_id: int, patch: dict) -> dict:
    factory = _require_db()
    patch = dict(patch)
    if "name" in patch:
        patch["name"] = validate_provider_name(patch["name"])
    if "api_provider" in patch:
        patch["api_provider"] = validate_api_provider(patch["api_provider"])
    if "sort_order" in patch:
        patch["sort_order"] = validate_sort_order(patch["sort_order"])
    try:
        async with factory() as session, session.begin():
            row, _ = await _lock_mutable_route(session, provider_id=provider_id)
            if row is None:
                raise ProviderNotFoundError(f"프로바이더 {provider_id} 를 찾을 수 없습니다")
            current_auth_mode = getattr(row, "auth_mode", "api_key")
            target_auth_mode = str(patch.get("auth_mode", current_auth_mode)).strip()
            target_provider_type = str(patch.get("provider_type", row.provider_type)).strip()
            if (current_auth_mode != "api_key" or target_auth_mode != "api_key") and (
                target_auth_mode != current_auth_mode or target_provider_type != row.provider_type
            ):
                raise ProviderValidationError("구독 인증 방식 또는 provider_type은 PATCH로 전환할 수 없습니다")
            if target_provider_type != row.provider_type or target_auth_mode != current_auth_mode or "api_base" in patch:
                media_kinds = (await session.execute(select(LlmModel.model_kind).where(
                    LlmModel.provider_id == provider_id, LlmModel.model_kind != "text"
                ))).scalars().all()
                for kind in media_kinds:
                    if kind not in MODEL_KINDS or target_provider_type not in _MEDIA_PROVIDERS or target_auth_mode != "api_key" or not direct_provider_route(target_provider_type, patch.get("api_base", row.api_base)):
                        raise ProviderValidationError("media 모델이 있는 프로바이더는 지원하지 않는 인증/유형/base로 전환할 수 없습니다")
            validate_provider_auth_configuration(
                provider_type=target_provider_type,
                auth_mode=target_auth_mode,
                api_base=patch.get("api_base", row.api_base),
                api_key=patch.get("api_key"),
                api_key_env=patch.get("api_key_env", row.api_key_env),
                billing_admin_key=patch.get("billing_admin_key"),
            )
            current_billing_capability = billing_capability_for(row.provider_type, current_auth_mode, row.api_base)
            target_billing_capability = billing_capability_for(
                target_provider_type,
                target_auth_mode,
                patch.get("api_base", row.api_base),
            )
            if "name" in patch:
                row.name = patch["name"]
            if "api_provider" in patch:
                row.api_provider = patch["api_provider"]
            if "sort_order" in patch:
                row.sort_order = patch["sort_order"]
            if patch.get("provider_type"):
                row.provider_type = target_provider_type
            if "auth_mode" in patch:
                row.auth_mode = target_auth_mode
            if "api_base" in patch:
                row.api_base = patch["api_base"] or None
            if "api_key" in patch:
                row.encrypted_api_key = encrypt_llm_provider_key(patch["api_key"]) if patch["api_key"] else None
            if "api_key_env" in patch:
                row.api_key_env = normalize_api_key_env(patch["api_key_env"])
            if "billing_admin_key" in patch:
                row.encrypted_billing_admin_key = (
                    encrypt_llm_provider_billing_admin_key(patch["billing_admin_key"])
                    if patch["billing_admin_key"]
                    else None
                )
            elif target_billing_capability != current_billing_capability:
                row.encrypted_billing_admin_key = None
            if patch.get("margin_multiplier") is not None:
                row.margin_multiplier = _to_decimal(patch["margin_multiplier"], "margin_multiplier")
            if "models_dev_provider_id" in patch:
                row.models_dev_provider_id = patch["models_dev_provider_id"] or None
            if patch.get("is_active") is not None:
                row.is_active = bool(patch["is_active"])
            await session.flush()
            return _provider_public(row)
    except IntegrityError as exc:
        raise ProviderValidationError("프로바이더 이름이 중복됩니다") from exc
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def delete_provider(provider_id: int) -> None:
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            row, _ = await _lock_mutable_route(session, provider_id=provider_id)
            if row is None:
                raise ProviderNotFoundError(f"프로바이더 {provider_id} 를 찾을 수 없습니다")
            await session.delete(row)
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


# ---------------------------------------------------------------------------
# 모델 CRUD
# ---------------------------------------------------------------------------
async def create_model(
    *,
    provider_id: int,
    model_name: str,
    display_name: str | None = None,
    sort_order: int = 0,
    model_kind: str = "text",
    media_pricing: dict | None = None,
    input_price_per_million=None,
    output_price_per_million=None,
    cache_read_price_per_million=None,
    cache_write_price_per_million=None,
    cache_write_1h_price_per_million=None,
    capabilities: dict | None = None,
    is_active: bool = True,
) -> dict:
    factory = _require_db()
    if not model_name or not model_name.strip():
        raise ProviderValidationError("model_name 은 필수입니다")
    sort_order = validate_sort_order(sort_order)
    media_pricing = _validate_kind_prices(model_kind, media_pricing, (
        input_price_per_million, output_price_per_million, cache_read_price_per_million,
        cache_write_price_per_million, cache_write_1h_price_per_million,
    ))
    input_price, output_price = _validate_text_prices(input_price_per_million, output_price_per_million)
    # Cache rates are optional and independent of each other and of input/output prices.
    cache_prices = _validate_cache_prices(
        {
            "cache_read_price_per_million": cache_read_price_per_million,
            "cache_write_price_per_million": cache_write_price_per_million,
            "cache_write_1h_price_per_million": cache_write_1h_price_per_million,
        }
    )
    try:
        async with factory() as session, session.begin():
            subscription_providers = await _lock_subscription_namespaces(session)
            provider = subscription_providers.get(provider_id) or await session.get(LlmProvider, provider_id)
            if provider is None:
                raise ProviderValidationError(f"프로바이더 {provider_id} 가 존재하지 않습니다")
            if model_kind != "text":
                provider = (await session.execute(
                    select(LlmProvider).where(LlmProvider.id == provider_id).with_for_update()
                    .execution_options(populate_existing=True)
                )).scalar_one()
            _validate_model_kind(provider, model_kind)
            if provider.provider_type == "perplexity" and getattr(provider, "auth_mode", "api_key") == "api_key":
                provider = (
                    await session.execute(select(LlmProvider).where(LlmProvider.id == provider_id).with_for_update())
                ).scalar_one()
            auth_mode = getattr(provider, "auth_mode", "api_key")
            namespace_provider_ids = tuple(
                candidate.id for candidate in subscription_providers.values() if candidate.auth_mode == auth_mode
            )
            canonical_name = canonical_subscription_model_name(model_name, auth_mode)
            if provider.provider_type == "perplexity" and auth_mode == "api_key":
                canonical_name = perplexity_route_model_name(canonical_name)
                await _reject_duplicate_perplexity_model(
                    session,
                    provider_id=provider_id,
                    api_name=api_model_name(canonical_name, "perplexity"),
                )
            await _reject_duplicate_subscription_model(
                session,
                namespace_provider_ids=namespace_provider_ids,
                model_name=canonical_name,
            )
            row = LlmModel(
                provider_id=provider_id,
                model_name=canonical_name,
                display_name=(display_name or None),
                sort_order=sort_order,
                model_kind=model_kind,
                media_pricing=media_pricing,
                input_price=input_price,
                output_price=output_price,
                **cache_prices,
                price_source=(
                    ("manual" if media_pricing else None)
                    if model_kind != "text"
                    else "manual"
                    if input_price is not None or output_price is not None
                    else None
                ),
                capabilities=(capabilities or None),
                capability_source=("override" if capabilities else None),
                is_active=is_active,
            )
            session.add(row)
            await session.flush()
            effective_input, effective_output, effective_source, _ = _resolved_base_prices(row, provider)
            return _model_public(
                row,
                effective_input_price_per_million=_per_million_price(effective_input),
                effective_output_price_per_million=_per_million_price(effective_output),
                effective_price_source=effective_source,
                provider_type=provider.provider_type,
                api_provider=provider.api_provider,
                provider_sort_order=provider.sort_order,
                auth_mode=auth_mode,
                api_key_configured=api_key_source(provider) is not None,
                api_base=provider.api_base,
            )
    except IntegrityError as exc:
        raise ProviderValidationError("프로바이더 내 model_name 이 중복됩니다") from exc
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def list_models(*, active_only: bool = False) -> list[dict]:
    factory = _require_db()
    try:
        async with factory() as session:
            stmt = (
                select(LlmModel, LlmProvider)
                .join(LlmProvider, LlmProvider.id == LlmModel.provider_id)
                .order_by(LlmProvider.sort_order, LlmProvider.id, LlmModel.sort_order, LlmModel.id)
            )
            if active_only:
                stmt = stmt.where(LlmModel.is_active.is_(True))
            rows = (await session.execute(stmt)).all()
            public_models: list[dict] = []
            for model, provider in rows:
                effective_input, effective_output, effective_source, _ = _resolved_base_prices(model, provider)
                public_models.append(_model_public(
                    model,
                    effective_input_price_per_million=_per_million_price(effective_input),
                    effective_output_price_per_million=_per_million_price(effective_output),
                    effective_price_source=effective_source,
                    provider_type=provider.provider_type,
                    api_provider=provider.api_provider,
                    provider_sort_order=provider.sort_order,
                    auth_mode=getattr(provider, "auth_mode", "api_key"),
                    api_key_configured=api_key_source(provider) is not None,
                    api_base=provider.api_base,
                ))
            return public_models
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def reorder_models(
    *, provider_id: int, expected_model_ids: list[int], model_ids: list[int],
) -> None:
    """Compare and replace a provider's complete display order in one transaction."""
    if type(provider_id) is not int or not 1 <= provider_id <= 9223372036854775807:
        raise ProviderValidationError("provider_id는 양의 정수여야 합니다")
    validate_model_order(expected_model_ids)
    validate_model_order(model_ids)
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            provider, models = await _lock_mutable_route(session, provider_id=provider_id)
            if provider is None:
                raise ProviderNotFoundError(f"프로바이더 {provider_id} 를 찾을 수 없습니다")
            current_ids = [model.id for model in sorted(models, key=lambda model: (model.sort_order, model.id))]
            if expected_model_ids != current_ids or set(model_ids) != set(current_ids):
                raise ModelOrderConflictError("모델 순서 또는 목록이 변경되었습니다. 전체 목록을 다시 조회하세요")
            rows_by_id = {model.id: model for model in models}
            for rank, model_id in enumerate(model_ids):
                row = rows_by_id[model_id]
                if row.sort_order != rank:
                    # Rank is not a configuration/price version change, including legacy media rows.
                    await session.execute(
                        update(LlmModel)
                        .where(LlmModel.id == model_id)
                        .values(sort_order=rank, updated_at=row.updated_at)
                    )
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def update_model(model_id: int, patch: dict) -> dict:
    factory = _require_db()
    if "sort_order" in patch:
        validate_sort_order(patch["sort_order"])
    has_input_price = "input_price_per_million" in patch
    has_output_price = "output_price_per_million" in patch
    # Present key → set (null clears); absent key → unchanged.
    cache_prices = _validate_cache_prices(patch)
    try:
        async with factory() as session, session.begin():
            subscription_providers = await _lock_subscription_namespaces(session)
            provider_id = await session.scalar(select(LlmModel.provider_id).where(LlmModel.id == model_id))
            if provider_id is None:
                raise ProviderNotFoundError(f"모델 {model_id} 를 찾을 수 없습니다")
            locked_subscription_provider = subscription_providers.get(provider_id)
            auth_mode = (
                locked_subscription_provider.auth_mode
                if locked_subscription_provider is not None
                else await session.scalar(select(LlmProvider.auth_mode).where(LlmProvider.id == provider_id))
            )
            if auth_mode is None:
                raise ProviderNotFoundError(f"프로바이더 {provider_id} 를 찾을 수 없습니다")
            namespace_provider_ids = tuple(
                candidate.id for candidate in subscription_providers.values() if candidate.auth_mode == auth_mode
            )
            provider, models = await _lock_mutable_route(session, provider_id=provider_id, model_ids={model_id})
            if provider is None:
                raise ProviderNotFoundError(f"프로바이더 {provider_id} 를 찾을 수 없습니다")
            row = models[0]
            target_kind = patch.get("model_kind", getattr(row, "model_kind", "text"))
            _validate_model_kind(provider, target_kind)
            media_pricing = patch.get("media_pricing", getattr(row, "media_pricing", None))
            token_prices = (
                patch.get("input_price_per_million", _per_million_price(row.input_price)),
                patch.get("output_price_per_million", _per_million_price(row.output_price)),
                *(patch.get(field, _per_million_price(getattr(row, column, None))) for field, column in (
                    ("cache_read_price_per_million", "cache_read_price"),
                    ("cache_write_price_per_million", "cache_write_price"),
                    ("cache_write_1h_price_per_million", "cache_write_1h_price"),
                )),
            )
            media_pricing = _validate_kind_prices(target_kind, media_pricing, token_prices)
            if target_kind != getattr(row, "model_kind", "text"):
                if row.is_title_model or row.is_memory_model:
                    raise ProviderValidationError("title/memory 모델은 text 종류만 허용합니다")
                row.model_kind = target_kind
                row.models_dev_model_id = None
                row.price_metadata = None
                row.price_source = None
            if "media_pricing" in patch:
                row.media_pricing = media_pricing
                if target_kind != "text":
                    row.price_source = "manual" if media_pricing else None
            if patch.get("model_name"):
                canonical_name = canonical_subscription_model_name(str(patch["model_name"]), auth_mode)
                if provider.provider_type == "perplexity" and auth_mode == "api_key":
                    encoded_name = perplexity_route_model_name(canonical_name)
                    public_name = api_model_name(encoded_name, "perplexity")
                    await _reject_duplicate_perplexity_model(
                        session,
                        provider_id=provider_id,
                        api_name=public_name,
                        current_model_id=model_id,
                    )
                    if public_name != api_model_name(row.model_name, "perplexity"):
                        row.model_name = encoded_name
                else:
                    await _reject_duplicate_subscription_model(
                        session,
                        namespace_provider_ids=namespace_provider_ids,
                        model_name=canonical_name,
                        current_model_id=model_id,
                    )
                    row.model_name = canonical_name
            if "display_name" in patch:
                row.display_name = patch["display_name"] or None
            if "sort_order" in patch:
                if len(patch) == 1:
                    # Legacy manual/media price versions use updated_at; rank is not a price change.
                    await session.execute(
                        update(LlmModel)
                        .where(LlmModel.id == model_id)
                        .values(sort_order=patch["sort_order"], updated_at=row.updated_at)
                    )
                else:
                    row.sort_order = patch["sort_order"]
            if has_input_price or has_output_price:
                input_price, output_price = _validate_text_prices(token_prices[0], token_prices[1])
                if has_input_price:
                    row.input_price = input_price
                if has_output_price:
                    row.output_price = output_price
                if target_kind == "text":
                    row.price_source = "manual" if input_price is not None or output_price is not None else None
                    row.price_metadata = None
            for column, price in cache_prices.items():
                setattr(row, column, price)
            if "capabilities" in patch:
                row.capabilities = patch["capabilities"] or None
                row.capability_source = "override" if patch["capabilities"] else None
            if patch.get("is_active") is not None:
                row.is_active = bool(patch["is_active"])
            await session.flush()
            effective_input, effective_output, effective_source, _ = _resolved_base_prices(row, provider)
            return _model_public(
                row,
                effective_input_price_per_million=_per_million_price(effective_input),
                effective_output_price_per_million=_per_million_price(effective_output),
                effective_price_source=effective_source,
                provider_type=provider.provider_type,
                api_provider=provider.api_provider,
                provider_sort_order=provider.sort_order,
                auth_mode=auth_mode,
                api_key_configured=api_key_source(provider) is not None,
                api_base=provider.api_base,
            )
    except IntegrityError as exc:
        raise ProviderValidationError("프로바이더 내 model_name 이 중복됩니다") from exc
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def import_models_dev_prices(
    *,
    local_provider_id: int,
    models_dev_provider_id: str,
    selections: list[dict[str, object]],
    catalog: ModelsDevCatalog,
) -> list[dict]:
    """Atomically apply administrator-selected exact models.dev price pairs."""
    if not 1 <= len(selections) <= 500:
        raise ProviderValidationError("selections는 1개 이상 500개 이하여야 합니다")
    external_provider = catalog.providers.get(models_dev_provider_id)
    if external_provider is None:
        raise ProviderValidationError("models.dev provider를 찾을 수 없습니다")

    selected_external: dict[int, str] = {}
    for selection in selections:
        local_model_id = selection.get("local_model_id")
        external_model_id = selection.get("models_dev_model_id")
        if not isinstance(local_model_id, int) or not isinstance(external_model_id, str) or not external_model_id:
            raise ProviderValidationError("models.dev import selection 형식이 올바르지 않습니다")
        if local_model_id in selected_external:
            raise ProviderValidationError("같은 local_model_id를 중복 선택할 수 없습니다")
        external_model = external_provider.models.get(external_model_id)
        if external_model is None or not external_model.price_available:
            raise ProviderValidationError("선택한 models.dev 모델에 완전한 input/output 가격이 없습니다")
        selected_external[local_model_id] = external_model_id

    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            provider, local_models = await _lock_mutable_route(
                session,
                provider_id=local_provider_id,
                model_ids=set(selected_external),
            )
            if provider is None:
                raise ProviderNotFoundError(f"프로바이더 {local_provider_id} 를 찾을 수 없습니다")
            if any(getattr(model, "model_kind", "text") != "text" for model in local_models):
                raise ProviderValidationError("models.dev 가격 import는 text 모델만 지원합니다")
            if any(model.price_source == "manual" for model in local_models):
                raise ModelsDevImportConflictError("수동 확정 가격 모델은 models.dev import로 덮어쓸 수 없습니다")

            if provider.models_dev_provider_id and provider.models_dev_provider_id != models_dev_provider_id:
                existing_imports = (
                    (
                        await session.execute(
                            select(LlmModel.id).where(
                                LlmModel.provider_id == local_provider_id,
                                LlmModel.price_source == "models.dev",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if set(existing_imports) - set(selected_external):
                    raise ModelsDevImportConflictError(
                        "다른 models.dev provider로 변경하려면 기존 imported model을 모두 선택해야 합니다"
                    )
                provider_models = (
                    (await session.execute(select(LlmModel).where(LlmModel.provider_id == local_provider_id)))
                    .scalars()
                    .all()
                )
                for model in provider_models:
                    if model.id not in selected_external and model.price_source != "models.dev":
                        model.models_dev_model_id = None

            provider.models_dev_provider_id = models_dev_provider_id
            rows_by_id = {model.id: model for model in local_models}
            for local_model_id, external_model_id in selected_external.items():
                external_model = external_provider.models[external_model_id]
                row = rows_by_id[local_model_id]
                row.models_dev_model_id = external_model_id
                row.input_price = _per_token_price(external_model.input_price_per_million, "input_price_per_million")
                row.output_price = _per_token_price(external_model.output_price_per_million, "output_price_per_million")
                row.price_source = "models.dev"
                if row.capability_source != "override" and external_model.capabilities is not None:
                    row.capabilities = external_model.capabilities
                    row.capability_source = "models_dev"
                row.price_metadata = {
                    "source_url": catalog.source_url,
                    "fetched_at": catalog.fetched_at,
                    "last_updated": external_model.last_updated,
                    "cost": _json_decimal_strings(external_model.cost),
                    "unsupported_price_fields": external_model.unsupported_price_fields,
                }
            await session.flush()
            public_models: list[dict] = []
            for local_model_id in selected_external:
                row = rows_by_id[local_model_id]
                effective_input, effective_output, effective_source, _ = _resolved_base_prices(row, provider)
                public_models.append(_model_public(
                    row,
                    effective_input_price_per_million=_per_million_price(effective_input),
                    effective_output_price_per_million=_per_million_price(effective_output),
                    effective_price_source=effective_source,
                    provider_type=provider.provider_type,
                    api_provider=provider.api_provider,
                    provider_sort_order=provider.sort_order,
                    auth_mode=getattr(provider, "auth_mode", "api_key"),
                    api_key_configured=api_key_source(provider) is not None,
                    api_base=provider.api_base,
                ))
            return public_models
    except IntegrityError as exc:
        raise ModelsDevImportConflictError("models.dev 가격 import 저장 충돌") from exc
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def delete_model(model_id: int) -> None:
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            provider_id = await session.scalar(select(LlmModel.provider_id).where(LlmModel.id == model_id))
            if provider_id is None:
                raise ProviderNotFoundError(f"모델 {model_id} 를 찾을 수 없습니다")
            provider, models = await _lock_mutable_route(session, provider_id=provider_id, model_ids={model_id})
            if provider is None:
                raise ProviderNotFoundError(f"프로바이더 {provider_id} 를 찾을 수 없습니다")
            await session.delete(models[0])
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def set_title_model(model_id: int | None) -> None:
    """대화 제목 자동 요약에 쓸 모델 1개를 지정. 앱 레벨에서 최대 1개만 True 로 유지.

    model_id=None 이면 지정 해제(모두 False). 대상이 없으면 ProviderNotFoundError.
    """
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            if model_id is not None:
                target = await session.get(LlmModel, model_id)
                if target is None:
                    raise ProviderNotFoundError(f"모델 {model_id} 를 찾을 수 없습니다")
                if getattr(target, "model_kind", "text") != "text":
                    raise ProviderValidationError("제목 요약은 text 모델만 사용할 수 있습니다")
            # 먼저 전부 해제 후 대상만 True (단일 보장)
            await session.execute(update(LlmModel).values(is_title_model=False))
            if model_id is not None:
                await session.execute(update(LlmModel).where(LlmModel.id == model_id).values(is_title_model=True))
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def set_memory_model(model_id: int | None) -> None:
    """채팅 후 사용자 메모리 자동 추출에 쓸 초소형 모델 1개 지정(또는 해제). 앱 레벨 최대 1개."""
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            if model_id is not None:
                target = await session.get(LlmModel, model_id)
                if target is None:
                    raise ProviderNotFoundError(f"모델 {model_id} 를 찾을 수 없습니다")
                if getattr(target, "model_kind", "text") != "text":
                    raise ProviderValidationError("메모리 추출은 text 모델만 사용할 수 있습니다")
            await session.execute(update(LlmModel).values(is_memory_model=False))
            if model_id is not None:
                await session.execute(update(LlmModel).where(LlmModel.id == model_id).values(is_memory_model=True))
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc
