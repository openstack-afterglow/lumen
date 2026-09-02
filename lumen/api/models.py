"""빌트인 AI 채팅 관리자 — LLM 프로바이더/모델 CRUD. 전 엔드포인트 require_admin.

프로바이더 api_key 는 요청으로만 받고 응답에는 절대 반환하지 않는다(has_api_key 불리언만).
서비스 계층(provider_store)이 AES-256-GCM 으로 암호화 저장한다.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from lumen.auth import require_admin
from lumen.services import model_discovery, models_dev
from lumen.services.providers import credentials, errors, repository

_PER_TOKEN_QUANTUM = Decimal("0.0000000001")
_TOKENS_PER_MILLION = Decimal("1000000")
router = APIRouter(dependencies=[Depends(require_admin)])


# --- 프로바이더 ---
class ProviderCreateRequest(BaseModel):
    name: str = Field(..., max_length=100)
    provider_type: str = Field(default="openai", max_length=40)  # litellm custom_llm_provider
    api_base: str | None = Field(default=None, max_length=255)
    api_key: str | None = Field(default=None, max_length=500)
    api_key_env: str | None = Field(default=None, max_length=128)
    margin_multiplier: float = Field(default=1.0, ge=0)
    is_active: bool = True

    @field_validator("api_key_env")
    @classmethod
    def validate_api_key_env(cls, value: str | None) -> str | None:
        return credentials.normalize_api_key_env(value)


class ProviderUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    provider_type: str | None = Field(default=None, max_length=40)
    api_base: str | None = Field(default=None, max_length=255)
    api_key: str | None = Field(default=None, max_length=500)
    api_key_env: str | None = Field(default=None, max_length=128)
    margin_multiplier: float | None = Field(default=None, ge=0)
    is_active: bool | None = None

    @field_validator("api_key_env")
    @classmethod
    def validate_api_key_env(cls, value: str | None) -> str | None:
        return credentials.normalize_api_key_env(value)


class ProviderResponse(BaseModel):
    id: int
    name: str
    provider_type: str
    api_base: str | None
    has_api_key: bool
    api_key_source: str | None = None
    api_key_env: str | None = None
    is_active: bool
    margin_multiplier: float
    created_at: str | None
    updated_at: str | None
    models_dev_provider_id: str | None = None


# --- 모델 ---
class CapabilitiesInput(BaseModel):
    """관리자 능력 수동 오버라이드(전체 dict). model_dump() 이 canonical 형태를 만든다."""

    vision: bool = False
    reasoning: bool = False
    tool_call: bool = False
    attachment: bool = False
    modalities: dict | None = None
    reasoning_options: list = Field(default_factory=list)
    context_limit: int | None = Field(default=None, ge=0)

    model_config = {"extra": "forbid"}


class ModelCreateRequest(BaseModel):
    provider_id: int
    model_name: str = Field(..., max_length=190)
    display_name: str | None = Field(default=None, max_length=150)
    input_price_per_million: Decimal | None = Field(default=None, ge=0)
    output_price_per_million: Decimal | None = Field(default=None, ge=0)
    capabilities: CapabilitiesInput | None = None
    is_active: bool = True

    model_config = {"protected_namespaces": (), "extra": "forbid"}

    @field_validator("input_price_per_million", "output_price_per_million")
    @classmethod
    def _finite_precise_price(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("가격은 유한한 숫자여야 합니다")
        if (
            value is not None
            and value != 0
            and (value / _TOKENS_PER_MILLION).quantize(_PER_TOKEN_QUANTUM, rounding=ROUND_HALF_UP) == 0
        ):
            raise ValueError("가격이 저장 정밀도보다 작습니다")
        return value

    @model_validator(mode="after")
    def _price_pair(self):
        if (self.input_price_per_million is None) != (self.output_price_per_million is None):
            raise ValueError("입력·출력 가격은 함께 설정하거나 함께 비워야 합니다")
        return self


class ModelUpdateRequest(BaseModel):
    model_name: str | None = Field(default=None, max_length=190)
    display_name: str | None = Field(default=None, max_length=150)
    input_price_per_million: Decimal | None = Field(default=None, ge=0)
    output_price_per_million: Decimal | None = Field(default=None, ge=0)
    capabilities: CapabilitiesInput | None = None
    is_active: bool | None = None

    model_config = {"protected_namespaces": (), "extra": "forbid"}

    @field_validator("input_price_per_million", "output_price_per_million")
    @classmethod
    def _finite_precise_price(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("가격은 유한한 숫자여야 합니다")
        if (
            value is not None
            and value != 0
            and (value / _TOKENS_PER_MILLION).quantize(_PER_TOKEN_QUANTUM, rounding=ROUND_HALF_UP) == 0
        ):
            raise ValueError("가격이 저장 정밀도보다 작습니다")
        return value

    @model_validator(mode="after")
    def _price_pair(self):
        price_fields = {"input_price_per_million", "output_price_per_million"}
        if self.model_fields_set & price_fields and (
            not price_fields <= self.model_fields_set
            or (self.input_price_per_million is None) != (self.output_price_per_million is None)
        ):
            raise ValueError("입력·출력 가격은 함께 설정하거나 함께 비워야 합니다")
        return self


class ModelResponse(BaseModel):
    id: int
    provider_id: int
    model_name: str
    display_name: str | None
    is_active: bool
    is_title_model: bool = False
    is_memory_model: bool = False
    input_price_per_million: Decimal | None = None
    output_price_per_million: Decimal | None = None
    effective_input_price_per_million: Decimal | None = None
    effective_output_price_per_million: Decimal | None = None
    effective_price_source: str | None = None
    models_dev_model_id: str | None = None
    price_source: str | None = None
    capabilities: dict | None = None
    capability_source: str | None = None
    effective_capabilities: dict | None = None
    effective_capability_source: str | None = None
    created_at: str | None
    updated_at: str | None

    model_config = {"protected_namespaces": ()}


class ModelsDevImportSelection(BaseModel):
    local_model_id: int
    models_dev_model_id: str = Field(..., min_length=1, max_length=190)


class ModelsDevImportRequest(BaseModel):
    local_provider_id: int
    models_dev_provider_id: str = Field(..., min_length=1, max_length=100)
    selections: list[ModelsDevImportSelection] = Field(..., min_length=1, max_length=500)

    model_config = {"extra": "forbid"}


class TitleModelRequest(BaseModel):
    """대화 제목 요약에 쓸 모델 지정. model_id=None 이면 해제."""

    model_id: int | None = None

    model_config = {"protected_namespaces": ()}


class MemoryModelRequest(BaseModel):
    """채팅 후 사용자 메모리 자동 추출에 쓸 초소형 모델 지정. model_id=None 이면 해제."""

    model_id: int | None = None

    model_config = {"protected_namespaces": ()}


def _map_storage(exc: Exception) -> HTTPException:
    return HTTPException(status_code=503, detail=str(exc))


# ---------------------------------------------------------------------------
# 프로바이더 엔드포인트
# ---------------------------------------------------------------------------
@router.get("/admin/providers", response_model=list[ProviderResponse])
async def list_providers():
    try:
        return await repository.list_providers()
    except errors.ChatStorageUnavailable as exc:
        raise _map_storage(exc) from exc


@router.post("/admin/providers", response_model=ProviderResponse, status_code=201)
async def create_provider(payload: ProviderCreateRequest):
    try:
        return await repository.create_provider(**payload.model_dump())
    except errors.ProviderValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except errors.ChatStorageUnavailable as exc:
        raise _map_storage(exc) from exc


@router.patch("/admin/providers/{provider_id}", response_model=ProviderResponse)
async def update_provider(provider_id: int, payload: ProviderUpdateRequest):
    try:
        return await repository.update_provider(provider_id, payload.model_dump(exclude_unset=True))
    except errors.ProviderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except errors.ActiveRunConfigurationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except errors.ProviderValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except errors.ChatStorageUnavailable as exc:
        raise _map_storage(exc) from exc


@router.delete("/admin/providers/{provider_id}", status_code=204)
async def delete_provider(provider_id: int):
    try:
        await repository.delete_provider(provider_id)
    except errors.ProviderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except errors.ActiveRunConfigurationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except errors.ChatStorageUnavailable as exc:
        raise _map_storage(exc) from exc


@router.get("/admin/providers/{provider_id}/available-models")
async def available_models(provider_id: int):
    """프로바이더 API(또는 litellm 정적 목록)에서 사용 가능한 모델 id 목록을 조회. 등록 전 선택/필터용."""
    try:
        return await model_discovery.discover_models(provider_id)
    except errors.ProviderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except errors.ChatStorageUnavailable as exc:
        raise _map_storage(exc) from exc


# ---------------------------------------------------------------------------
# 모델 엔드포인트
# ---------------------------------------------------------------------------
@router.get("/admin/models", response_model=list[ModelResponse])
async def list_models(active_only: bool = False):
    try:
        return await repository.list_models(active_only=active_only)
    except errors.ChatStorageUnavailable as exc:
        raise _map_storage(exc) from exc


@router.get("/admin/models/pricing/models-dev/providers")
async def list_models_dev_providers(
    local_provider_id: int,
    refresh: bool = False,
):
    try:
        registered_providers = await repository.list_providers()
        current_provider = next(
            (provider for provider in registered_providers if provider["id"] == local_provider_id),
            None,
        )
        if current_provider is None:
            raise errors.ProviderNotFoundError(f"프로바이더 {local_provider_id} 를 찾을 수 없습니다")
        catalog = await models_dev.get_catalog(refresh=refresh)
        current_provider_matches = models_dev.matching_provider_ids(catalog, current_provider)
        providers = models_dev.registered_provider_list(
            catalog,
            registered_providers,
            current_provider_id=local_provider_id,
        )
    except models_dev.ModelsDevCatalogError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except errors.ProviderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except errors.ChatStorageUnavailable as exc:
        raise _map_storage(exc) from exc
    return {
        "source_url": catalog.source_url,
        "fetched_at": catalog.fetched_at,
        "preferred_provider_ids": sorted(current_provider_matches),
        "providers": providers,
    }


@router.get("/admin/models/pricing/models-dev/providers/{models_dev_provider_id}")
async def get_models_dev_provider(models_dev_provider_id: str, refresh: bool = False):
    try:
        catalog = await models_dev.get_catalog(refresh=refresh)
    except models_dev.ModelsDevCatalogError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    details = models_dev.provider_detail(catalog, models_dev_provider_id)
    if details is None:
        raise HTTPException(status_code=404, detail="models.dev provider를 찾을 수 없습니다")
    models = details.pop("models")
    return {"source_url": catalog.source_url, "fetched_at": catalog.fetched_at, "provider": details, "models": models}


@router.post("/admin/models/pricing/models-dev/import", response_model=list[ModelResponse])
async def import_models_dev_prices(payload: ModelsDevImportRequest):
    try:
        catalog = await models_dev.get_catalog()
        return await repository.import_models_dev_prices(
            local_provider_id=payload.local_provider_id,
            models_dev_provider_id=payload.models_dev_provider_id,
            selections=[selection.model_dump() for selection in payload.selections],
            catalog=catalog,
        )
    except models_dev.ModelsDevCatalogError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except errors.ProviderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except errors.ModelsDevImportConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except errors.ActiveRunConfigurationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except errors.ProviderValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except errors.ChatStorageUnavailable as exc:
        raise _map_storage(exc) from exc


@router.put("/admin/title-model", status_code=204)
async def set_title_model(payload: TitleModelRequest):
    """대화 제목 자동 요약에 쓸 모델 1개를 지정(또는 해제). 요약 호출 비용은 시스템 부담."""
    try:
        await repository.set_title_model(payload.model_id)
    except errors.ProviderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except errors.ChatStorageUnavailable as exc:
        raise _map_storage(exc) from exc


@router.put("/admin/memory-model", status_code=204)
async def set_memory_model(payload: MemoryModelRequest):
    """채팅 후 사용자 메모리 자동 추출에 쓸 초소형 모델 1개 지정(또는 해제). 추출 비용은 시스템 부담."""
    try:
        await repository.set_memory_model(payload.model_id)
    except errors.ProviderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except errors.ChatStorageUnavailable as exc:
        raise _map_storage(exc) from exc


@router.post("/admin/models", response_model=ModelResponse, status_code=201)
async def create_model(payload: ModelCreateRequest):
    try:
        return await repository.create_model(**payload.model_dump())
    except errors.ProviderValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except errors.ChatStorageUnavailable as exc:
        raise _map_storage(exc) from exc


@router.patch("/admin/models/{model_id}", response_model=ModelResponse)
async def update_model(model_id: int, payload: ModelUpdateRequest):
    try:
        return await repository.update_model(model_id, payload.model_dump(exclude_unset=True))
    except errors.ProviderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except errors.ActiveRunConfigurationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except errors.ProviderValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except errors.ChatStorageUnavailable as exc:
        raise _map_storage(exc) from exc


@router.delete("/admin/models/{model_id}", status_code=204)
async def delete_model(model_id: int):
    try:
        await repository.delete_model(model_id)
    except errors.ProviderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except errors.ActiveRunConfigurationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except errors.ChatStorageUnavailable as exc:
        raise _map_storage(exc) from exc
