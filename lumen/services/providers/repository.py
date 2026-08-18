"""Provider and model administration repository."""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from lumen.crypto import encrypt_llm_provider_key
from lumen.db import mark_db_unhealthy
from lumen.models.chat_db import LlmModel, LlmProvider
from lumen.services.litellm_client import effective_prices_per_million
from lumen.services.models_dev import ModelsDevCatalog

from .errors import (
    ChatStorageUnavailable,
    ModelsDevImportConflictError,
    ProviderNotFoundError,
    ProviderValidationError,
)
from .pricing import (
    _json_decimal_strings,
    _model_public,
    _per_million_price,
    _per_token_price,
    _provider_public,
    _to_decimal,
    _validate_price_pair,
)
from .routing import _lock_mutable_route, _require_db


async def create_provider(
    *,
    name: str,
    provider_type: str = "openai",
    api_base: str | None = None,
    api_key: str | None = None,
    margin_multiplier=1.0,
    models_dev_provider_id: str | None = None,
    is_active: bool = True,
) -> dict:
    factory = _require_db()
    if not name or not name.strip():
        raise ProviderValidationError("name 은 필수입니다")
    row = LlmProvider(
        name=name.strip(),
        provider_type=(provider_type or "openai").strip(),
        api_base=(api_base or None),
        encrypted_api_key=(encrypt_llm_provider_key(api_key) if api_key else None),
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
            rows = (await session.execute(select(LlmProvider).order_by(LlmProvider.id))).scalars().all()
            return [_provider_public(row) for row in rows]
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def update_provider(provider_id: int, patch: dict) -> dict:
    factory = _require_db()
    try:
        async with factory() as session, session.begin():
            row, _ = await _lock_mutable_route(session, provider_id=provider_id)
            if row is None:
                raise ProviderNotFoundError(f"프로바이더 {provider_id} 를 찾을 수 없습니다")
            if patch.get("name"):
                row.name = str(patch["name"]).strip()
            if patch.get("provider_type"):
                row.provider_type = str(patch["provider_type"]).strip()
            if "api_base" in patch:
                row.api_base = patch["api_base"] or None
            if "api_key" in patch:
                row.encrypted_api_key = encrypt_llm_provider_key(patch["api_key"]) if patch["api_key"] else None
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
    input_price_per_million=None,
    output_price_per_million=None,
    capabilities: dict | None = None,
    is_active: bool = True,
) -> dict:
    factory = _require_db()
    if not model_name or not model_name.strip():
        raise ProviderValidationError("model_name 은 필수입니다")
    input_price, output_price = _validate_price_pair(input_price_per_million, output_price_per_million)
    row = LlmModel(
        provider_id=provider_id,
        model_name=model_name.strip(),
        display_name=(display_name or None),
        input_price=input_price,
        output_price=output_price,
        price_source="manual" if input_price is not None else None,
        capabilities=(capabilities or None),
        capability_source=("override" if capabilities else None),
        is_active=is_active,
    )
    try:
        async with factory() as session, session.begin():
            provider = await session.get(LlmProvider, provider_id)
            if provider is None:
                raise ProviderValidationError(f"프로바이더 {provider_id} 가 존재하지 않습니다")
            session.add(row)
            await session.flush()
            return _model_public(row, provider_type=provider.provider_type)
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
                .order_by(LlmModel.id)
            )
            if active_only:
                stmt = stmt.where(LlmModel.is_active.is_(True))
            rows = (await session.execute(stmt)).all()
            public_models: list[dict] = []
            for model, provider in rows:
                stored_input = _per_million_price(model.input_price)
                stored_output = _per_million_price(model.output_price)
                fallback_input, fallback_output = (
                    (None, None)
                    if stored_input is not None and stored_output is not None
                    else effective_prices_per_million(model.model_name, provider.provider_type)
                )
                effective_input = stored_input if stored_input is not None else fallback_input
                effective_output = stored_output if stored_output is not None else fallback_output
                if stored_input is not None and stored_output is not None:
                    effective_source = model.price_source
                elif effective_input is not None and effective_output is not None:
                    effective_source = "litellm" if stored_input is None and stored_output is None else "partial"
                else:
                    effective_source = "unpriced"
                public_models.append(
                    _model_public(
                        model,
                        effective_input_price_per_million=effective_input,
                        effective_output_price_per_million=effective_output,
                        effective_price_source=effective_source,
                        provider_type=provider.provider_type,
                    )
                )
            return public_models
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc


async def update_model(model_id: int, patch: dict) -> dict:
    factory = _require_db()
    has_input_price = "input_price_per_million" in patch
    has_output_price = "output_price_per_million" in patch
    if has_input_price != has_output_price:
        raise ProviderValidationError("입력·출력 가격은 함께 설정하거나 함께 비워야 합니다")
    try:
        async with factory() as session, session.begin():
            provider_id = await session.scalar(select(LlmModel.provider_id).where(LlmModel.id == model_id))
            if provider_id is None:
                raise ProviderNotFoundError(f"모델 {model_id} 를 찾을 수 없습니다")
            provider, models = await _lock_mutable_route(session, provider_id=provider_id, model_ids={model_id})
            if provider is None:
                raise ProviderNotFoundError(f"프로바이더 {provider_id} 를 찾을 수 없습니다")
            row = models[0]
            if patch.get("model_name"):
                row.model_name = str(patch["model_name"]).strip()
            if "display_name" in patch:
                row.display_name = patch["display_name"] or None
            if has_input_price:
                input_price, output_price = _validate_price_pair(
                    patch["input_price_per_million"], patch["output_price_per_million"]
                )
                row.input_price = input_price
                row.output_price = output_price
                row.price_source = "manual" if input_price is not None else None
                row.price_metadata = None
            if "capabilities" in patch:
                # 관리자 수동 오버라이드 — models.dev import 로 덮이지 않도록 source=override 고정.
                row.capabilities = patch["capabilities"] or None
                row.capability_source = "override" if patch["capabilities"] else None
            if patch.get("is_active") is not None:
                row.is_active = bool(patch["is_active"])
            await session.flush()
            return _model_public(row, provider_type=provider.provider_type)
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
            return [
                _model_public(rows_by_id[local_model_id], provider_type=provider.provider_type)
                for local_model_id in selected_external
            ]
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
            await session.execute(update(LlmModel).values(is_memory_model=False))
            if model_id is not None:
                await session.execute(update(LlmModel).where(LlmModel.id == model_id).values(is_memory_model=True))
    except OperationalError as exc:
        mark_db_unhealthy()
        raise ChatStorageUnavailable("chat DB 오류") from exc
