"""빌트인 AI 채팅 관리자 — LLM 프로바이더/모델 CRUD. 전 엔드포인트 require_admin.

프로바이더 api_key 는 요청으로만 받고 응답에는 절대 반환하지 않는다(has_api_key 불리언만).
서비스 계층(provider_store)이 AES-256-GCM 으로 암호화 저장한다.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_core import PydanticCustomError

from lumen.auth import require_admin
from lumen.services import model_discovery
from lumen.services.native_catalog import ModelKind, catalog_response, validate_embedding_metadata
from lumen.services.providers import accounts, billing, credentials, errors, repository, routing, subscriptions
from lumen.services.providers.pricing import domain_billing_readiness, frozen_token_pricing

_PER_TOKEN_QUANTUM = Decimal("0.0000000001")
_TOKENS_PER_MILLION = Decimal("1000000")
router = APIRouter(dependencies=[Depends(require_admin)])
_NO_STORE = {"Cache-Control": "no-store"}
_CLAUDE_SUBSCRIPTION_TOKEN = re.compile(r"sk-ant-oat[A-Za-z0-9._-]*\Z")


# --- 프로바이더 ---
class ProviderCreateRequest(BaseModel):
    name: str = Field(..., max_length=100)
    provider_type: str = Field(default="openai", max_length=40)
    api_provider: str = Field(default="openai", max_length=40)
    sort_order: int = Field(default=0, strict=True, ge=0, le=2147483647)
    auth_mode: Literal["api_key", "chatgpt_device", "anthropic_subscription"] = "api_key"
    api_base: str | None = Field(default=None, max_length=255)
    api_key: str | None = Field(default=None, max_length=500)
    api_key_env: str | None = Field(default=None, max_length=128)
    billing_admin_key: str | None = Field(default=None, max_length=1000)
    margin_multiplier: float = Field(default=1.0, ge=0)
    is_active: bool = True
    selection_policy: Literal["priority", "weighted"] = "priority"

    model_config = {"extra": "forbid"}

    @field_validator("name", mode="before")
    @classmethod
    def validate_name(cls, value):
        return repository.validate_provider_name(value)

    @field_validator("provider_type", mode="before")
    @classmethod
    def validate_provider_type(cls, value):
        try:
            return repository.validate_provider_type(value)
        except errors.UnsupportedProviderTypeError as exc:
            raise PydanticCustomError(exc.code, str(exc)) from None

    @field_validator("api_provider", mode="before")
    @classmethod
    def validate_api_provider(cls, value):
        return repository.validate_api_provider(value)

    @field_validator("api_key_env")
    @classmethod
    def validate_api_key_env(cls, value: str | None) -> str | None:
        return credentials.normalize_api_key_env(value)

    @model_validator(mode="after")
    def validate_auth_configuration(self):
        if "api_provider" not in self.model_fields_set:
            self.api_provider = repository.validate_api_provider(self.provider_type)
        try:
            repository.validate_provider_auth_configuration(
                provider_type=self.provider_type.strip(),
                auth_mode=self.auth_mode,
                api_base=self.api_base,
                api_key=self.api_key,
                api_key_env=self.api_key_env,
                billing_admin_key=self.billing_admin_key,
            )
        except errors.ProviderValidationError as exc:
            raise ValueError(str(exc)) from None
        return self


class ProviderUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    provider_type: str | None = Field(default=None, max_length=40)
    api_provider: str | None = Field(default=None, max_length=40)
    sort_order: int | None = Field(default=None, strict=True, ge=0, le=2147483647)
    auth_mode: Literal["api_key", "chatgpt_device", "anthropic_subscription"] | None = None
    api_base: str | None = Field(default=None, max_length=255)
    api_key: str | None = Field(default=None, max_length=500)
    api_key_env: str | None = Field(default=None, max_length=128)
    billing_admin_key: str | None = Field(default=None, max_length=1000)
    margin_multiplier: float | None = Field(default=None, ge=0)
    is_active: bool | None = None
    selection_policy: Literal["explicit", "priority", "weighted"] | None = None
    selected_credential_id: int | None = Field(default=None, strict=True, gt=0)

    model_config = {"extra": "forbid"}

    @field_validator("name", mode="before")
    @classmethod
    def validate_name(cls, value):
        return repository.validate_provider_name(value)

    @field_validator("provider_type", mode="before")
    @classmethod
    def validate_provider_type(cls, value):
        try:
            return repository.validate_provider_type(value)
        except errors.UnsupportedProviderTypeError as exc:
            raise PydanticCustomError(exc.code, str(exc)) from None

    @field_validator("api_provider", mode="before")
    @classmethod
    def validate_api_provider(cls, value):
        return repository.validate_api_provider(value)

    @field_validator("sort_order", mode="before")
    @classmethod
    def validate_sort_order(cls, value):
        return repository.validate_sort_order(value)

    @field_validator("api_key_env")
    @classmethod
    def validate_api_key_env(cls, value: str | None) -> str | None:
        return credentials.normalize_api_key_env(value)

    @model_validator(mode="after")
    def reject_subscription_secrets(self):
        if self.auth_mode in {"chatgpt_device", "anthropic_subscription"} and any(
            isinstance(value, str) and value.strip()
            for value in (self.api_base, self.api_key, self.api_key_env, self.billing_admin_key)
        ):
            raise ValueError("구독 프로바이더에는 API base/key/environment/admin credential을 설정할 수 없습니다")
        return self


class ProviderResponse(BaseModel):
    id: int
    name: str
    provider_type: str
    provider_family: Literal["native", "compatible"] = "native"
    api_provider: str
    sort_order: int = 0
    api_base: str | None
    auth_mode: Literal["api_key", "chatgpt_device", "anthropic_subscription"] = "api_key"
    has_credentials: bool = False
    auth_status: Literal["disconnected", "configured", "reauth_required"] = "disconnected"
    auth_expires_at: str | None = None
    has_api_key: bool
    api_key_source: str | None = None
    api_key_env: str | None = None
    has_billing_admin_key: bool = False
    is_active: bool
    margin_multiplier: float
    billing_capability: (
        Literal[
            "openai_admin_usage",
            "anthropic_admin_usage",
        ]
        | None
    ) = None
    created_at: str | None
    updated_at: str | None
    selection_policy: Literal["explicit", "priority", "weighted"] = "priority"
    selected_credential_id: int | None = None


class ProviderBillingBalance(BaseModel):
    currency: str
    total: Decimal
    granted: Decimal
    purchased: Decimal


class ProviderBillingIntegerPeriods(BaseModel):
    daily: int
    weekly: int
    monthly: int
    total: int


class ProviderBillingDecimalPeriods(BaseModel):
    daily: Decimal
    weekly: Decimal
    monthly: Decimal
    total: Decimal


class ProviderBillingTokenBreakdown(BaseModel):
    """Anthropic organization-report token categories; consistent rows sum to ``tokens``."""

    uncached_input: ProviderBillingIntegerPeriods
    cache_read: ProviderBillingIntegerPeriods
    cache_creation_5m: ProviderBillingIntegerPeriods
    cache_creation_1h: ProviderBillingIntegerPeriods
    output: ProviderBillingIntegerPeriods


class ProviderBillingLocalUsage(BaseModel):
    currency: Literal["USD"]
    requests: ProviderBillingIntegerPeriods
    tokens: ProviderBillingIntegerPeriods
    raw_cost: ProviderBillingDecimalPeriods
    token_breakdown: ProviderBillingTokenBreakdown | None = None


class ProviderBillingOptionalIntegerPeriods(BaseModel):
    daily: int | None
    weekly: int | None
    monthly: int | None
    total: int | None


class ProviderBillingOptionalDecimalPeriods(BaseModel):
    daily: Decimal | None
    weekly: Decimal | None
    monthly: Decimal | None
    total: Decimal | None


class ProviderBillingOptionalTokenBreakdown(BaseModel):
    uncached_input: ProviderBillingOptionalIntegerPeriods
    cache_read: ProviderBillingOptionalIntegerPeriods
    cache_creation_5m: ProviderBillingOptionalIntegerPeriods
    cache_creation_1h: ProviderBillingOptionalIntegerPeriods
    output: ProviderBillingOptionalIntegerPeriods


class ProviderBillingProviderUsage(BaseModel):
    source: Literal["openai_admin_usage", "anthropic_admin_usage"]
    currency: Literal["USD"]
    cost: ProviderBillingOptionalDecimalPeriods | None
    requests: ProviderBillingOptionalIntegerPeriods | None
    tokens: ProviderBillingOptionalIntegerPeriods | None
    # Anthropic organization usage report only; OpenAI reports no cache-creation split.
    token_breakdown: ProviderBillingOptionalTokenBreakdown | None = None


class ProviderBillingResponse(BaseModel):
    provider_id: int
    provider_name: str
    provider_type: str
    capability: (
        Literal[
            "openai_admin_usage",
            "anthropic_admin_usage",
        ]
        | None
    )
    status: Literal["available", "unavailable", "unsupported"]
    reason: (
        Literal[
            "billing_endpoint_unsupported",
            "provider_console_only",
            "provider_analytics_scope_mismatch",
            "credential_unavailable",
            "credential_not_configured",
            "admin_credential_unavailable",
            "admin_credential_not_configured",
            "admin_credential_rejected",
            "partial_provider_data",
            "provider_authorization_failed",
            "provider_request_failed",
            "provider_unavailable",
        ]
        | None
    )
    fetched_at: datetime
    billing_url: str | None
    usage_url: str | None
    has_billing_admin_key: bool
    local_usage: ProviderBillingLocalUsage
    provider_usage: ProviderBillingProviderUsage | None
    is_available: bool | None
    is_free_tier: bool | None
    limit: Decimal | None
    remaining: Decimal | None
    usage_total: Decimal | None
    usage_daily: Decimal | None
    usage_weekly: Decimal | None
    usage_monthly: Decimal | None
    balances: list[ProviderBillingBalance]


class DeviceAuthStartResponse(BaseModel):
    attempt_id: str
    status: Literal["pending"]
    verification_uri: str
    user_code: str
    expires_at: str
    interval_seconds: int


class DeviceAuthStatusResponse(BaseModel):
    attempt_id: str
    status: Literal["pending", "connected", "cancelled", "expired", "error"]
    expires_at: str
    interval_seconds: int


class SubscriptionTokenRequest(BaseModel):
    token: SecretStr
    expires_at: datetime | None = None

    model_config = {"extra": "forbid"}

    @field_validator("token")
    @classmethod
    def validate_token(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not 16 <= len(raw) <= 8192 or _CLAUDE_SUBSCRIPTION_TOKEN.fullmatch(raw) is None:
            raise ValueError("Claude 구독 token 형식이 올바르지 않습니다")
        return value


# --- 모델 ---
class CapabilitiesInput(BaseModel):
    """관리자 능력 수동 오버라이드(전체 dict). model_dump() 이 canonical 형태를 만든다."""

    vision: bool = False
    reasoning: bool = False
    tool_call: bool = False
    attachment: bool = False
    decisions: bool = False
    prompt_caching: bool | None = None
    structured_output: bool | None = None
    web_search: bool | None = None
    modalities: dict | None = None
    reasoning_options: list = Field(default_factory=list)
    context_limit: int | None = Field(default=None, ge=0)
    embedding: dict | None = None

    model_config = {"extra": "forbid"}

    @field_validator("embedding")
    @classmethod
    def validate_embedding(cls, value: dict | None) -> dict | None:
        return validate_embedding_metadata(value)


class ModelCreateRequest(BaseModel):
    provider_id: int
    model_name: str = Field(..., max_length=190)
    display_name: str | None = Field(default=None, max_length=150)
    sort_order: int = Field(default=0, strict=True, ge=0, le=2147483647)
    model_kind: ModelKind = "text"
    media_pricing: dict | None = None
    input_price_per_million: Decimal | None = Field(default=None, ge=0)
    output_price_per_million: Decimal | None = Field(default=None, ge=0)
    # Optional prompt-cache rates, independent of each other and of input/output prices.
    # Unset rates use the exact native catalog; cache read otherwise stays unpriced.
    # Text writes then inherit input (short TTL) and short-TTL writes (long TTL).
    cache_read_price_per_million: Decimal | None = Field(default=None, ge=0)
    cache_write_price_per_million: Decimal | None = Field(default=None, ge=0)
    cache_write_1h_price_per_million: Decimal | None = Field(default=None, ge=0)
    capabilities: CapabilitiesInput | None = None
    is_active: bool = True
    @model_validator(mode="after")
    def _kind_prices(self):
        # Media models may price text input/output/cache-read for a token basis; the
        # repository validates media_pricing per kind (text allows token_rates only).
        if self.model_kind not in {"text", "decision"} and any(getattr(self, field) is not None for field in (
            "cache_write_price_per_million", "cache_write_1h_price_per_million",
        )):
            raise ValueError("media 모델에는 cache write 가격을 설정할 수 없습니다")
        return self


    model_config = {"protected_namespaces": (), "extra": "forbid"}

    @field_validator(
        "input_price_per_million",
        "output_price_per_million",
        "cache_read_price_per_million",
        "cache_write_price_per_million",
        "cache_write_1h_price_per_million",
    )
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


class ModelUpdateRequest(BaseModel):
    model_name: str | None = Field(default=None, max_length=190)
    display_name: str | None = Field(default=None, max_length=150)
    sort_order: int | None = Field(default=None, strict=True, ge=0, le=2147483647)
    model_kind: ModelKind | None = None
    media_pricing: dict | None = None
    input_price_per_million: Decimal | None = Field(default=None, ge=0)
    output_price_per_million: Decimal | None = Field(default=None, ge=0)
    # Optional prompt-cache rates, independent of each other and of input/output prices.
    # Unset rates use the exact native catalog; cache read otherwise stays unpriced.
    # Text writes then inherit input (short TTL) and short-TTL writes (long TTL).
    cache_read_price_per_million: Decimal | None = Field(default=None, ge=0)
    cache_write_price_per_million: Decimal | None = Field(default=None, ge=0)
    cache_write_1h_price_per_million: Decimal | None = Field(default=None, ge=0)
    capabilities: CapabilitiesInput | None = None
    is_active: bool | None = None

    model_config = {"protected_namespaces": (), "extra": "forbid"}

    @field_validator("sort_order", mode="before")
    @classmethod
    def validate_sort_order(cls, value):
        return repository.validate_sort_order(value)

    @field_validator(
        "input_price_per_million",
        "output_price_per_million",
        "cache_read_price_per_million",
        "cache_write_price_per_million",
        "cache_write_1h_price_per_million",
    )
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


class ModelReorderRequest(BaseModel):
    provider_id: int = Field(strict=True, gt=0, le=9223372036854775807)
    expected_model_ids: list[Annotated[int, Field(strict=True, gt=0, le=9223372036854775807)]] = Field(
        min_length=1, max_length=500,
    )
    model_ids: list[Annotated[int, Field(strict=True, gt=0, le=9223372036854775807)]] = Field(
        min_length=1, max_length=500,
    )

    model_config = {"protected_namespaces": (), "extra": "forbid"}

    @field_validator("expected_model_ids", "model_ids")
    @classmethod
    def validate_model_order(cls, value):
        return repository.validate_model_order(value)


class ModelResponse(BaseModel):
    id: int
    provider_id: int
    model_name: str
    model_kind: ModelKind = "text"
    media_pricing: dict | None = None
    effective_media_pricing: dict | None = None
    effective_media_price_sources: dict = Field(default_factory=dict)
    effective_text_price_sources: dict[str, Literal["manual", "catalog"]] = Field(default_factory=dict)
    # Image rows only: priced choices this provider/model can execute, independent of credentials.
    image_pricing_readiness: dict | None = None
    api_model_name: str
    api_provider: str
    provider_type: str | None = None
    provider_family: Literal["native", "compatible"] = "native"
    price_origin: Literal["manual", "native_catalog"] | None = None
    catalog_version: str | None = None
    catalog_entry: dict | None = None
    provider_sort_order: int = 0
    sort_order: int = 0
    display_name: str | None
    is_active: bool
    is_title_model: bool = False
    is_memory_model: bool = False
    input_price_per_million: Decimal | None = None
    output_price_per_million: Decimal | None = None
    effective_input_price_per_million: Decimal | None = None
    effective_output_price_per_million: Decimal | None = None
    effective_price_source: str | None = None
    cache_read_price_per_million: Decimal | None = None
    cache_write_price_per_million: Decimal | None = None
    cache_write_1h_price_per_million: Decimal | None = None
    # Resolved rates new admissions freeze; the stored manual fields above stay nullable.
    effective_cache_read_price_per_million: Decimal | None = None
    effective_cache_write_price_per_million: Decimal | None = None
    effective_cache_write_1h_price_per_million: Decimal | None = None
    effective_cache_price_sources: dict[str, str | None] | None = None
    price_source: str | None = None
    capabilities: dict | None = None
    capability_source: str | None = None
    effective_capabilities: dict | None = None
    effective_capability_source: str | None = None
    # Text models only: {"ready", "reason_code"}; set capabilities.context_limit to fix context_limit_unknown.
    domain_billing: dict | None = None
    created_at: str | None
    updated_at: str | None

    model_config = {"protected_namespaces": ()}


class NativeCatalogEmbedding(BaseModel):
    """Published embedding dimensions; null means the provider did not publish a constraint."""

    default_dimensions: int | None
    supports_dimensions: bool
    min_dimensions: int | None
    max_dimensions: int | None
    allowed_dimensions: list[int] | None


class NativeCatalogTokenPrices(BaseModel):
    """Published USD prices per million tokens, preserved as decimal strings."""

    input: str | None = None
    output: str | None = None
    cache_read: str | None = None
    cache_write: str | None = None
    cache_write_long: str | None = None


class NativeCatalogLongContextPrices(NativeCatalogTokenPrices):
    threshold_tokens: int
    token_rates: dict[str, dict[str, str | None]] | None = None


class NativeCatalogTokenPricing(NativeCatalogTokenPrices):
    cache_write_ttl: str | None = None
    cache_write_long_ttl: str | None = None
    long_context: NativeCatalogLongContextPrices | None = None


class NativeCatalogModel(BaseModel):
    id: str
    aliases: list[str]
    kind: ModelKind
    display_name: str
    status: Literal["active", "preview", "deprecated"]
    context_window: int | None
    max_input_tokens: int | None = None
    max_output_tokens: int | None
    input_modalities: list[str] | None
    output_modalities: list[str] | None
    capabilities: dict[str, bool]
    reasoning_options: list[dict] | None = None
    embedding: NativeCatalogEmbedding | None = None
    token_pricing: NativeCatalogTokenPricing | None
    media_pricing: dict | None
    source_urls: list[str]
    verified_at: str
    notes: str | None


class NativeCatalogProvider(BaseModel):
    provider_type: Literal["openai", "anthropic", "gemini", "perplexity"]
    display_name: str
    pricing_sources: list[str]
    models: list[NativeCatalogModel]


class NativeCatalogResponse(BaseModel):
    schema_version: Literal[1]
    catalog_version: str
    providers: list[NativeCatalogProvider]


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


def _map_subscription_error(exc: Exception) -> HTTPException:
    if isinstance(exc, errors.ProviderNotFoundError):
        status_code = 404
        detail: object = str(exc)
    elif isinstance(exc, (errors.ActiveRunConfigurationConflict, errors.ProviderAuthAttemptConflict)):
        status_code = 409
        detail = str(exc)
    elif isinstance(exc, errors.ProviderConfigurationChangedError):
        status_code = 409
        detail = "프로바이더 인증 설정이 변경되었습니다"
    elif isinstance(exc, errors.ProviderValidationError):
        status_code = 400
        detail = str(exc)
    elif isinstance(exc, errors.ProviderSubscriptionError):
        status_code = exc.status_code
        detail = {"code": exc.code, "message": exc.message}
    else:
        status_code = 503
        detail = "구독 인증 저장소를 사용할 수 없습니다"
    return HTTPException(status_code=status_code, detail=detail, headers=_NO_STORE)


# ---------------------------------------------------------------------------
# 프로바이더 엔드포인트
# ---------------------------------------------------------------------------
@router.get("/admin/providers", response_model=list[ProviderResponse])
async def list_providers():
    try:
        return await repository.list_providers()
    except errors.ChatStorageUnavailable as exc:
        raise _map_storage(exc) from exc


@router.get(
    "/admin/providers/billing",
    response_model=list[ProviderBillingResponse],
)
async def list_provider_billing():
    try:
        return await billing.list_provider_billing()
    except errors.ChatStorageUnavailable as exc:
        raise _map_storage(exc) from exc


@router.post("/admin/providers", response_model=ProviderResponse, status_code=201)
async def create_provider(payload: ProviderCreateRequest):
    try:
        return await repository.create_provider(**payload.model_dump())
    except errors.UnsupportedProviderTypeError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc
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
    except errors.UnsupportedProviderTypeError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc
    except errors.ProviderValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except errors.ChatStorageUnavailable as exc:
        raise _map_storage(exc) from exc


@router.post(
    "/admin/providers/{provider_id}/auth/device",
    response_model=DeviceAuthStartResponse,
    status_code=201,
)
async def begin_provider_device_auth(
    provider_id: int,
    response: Response,
    admin_info: dict = Depends(require_admin),
):
    try:
        result = await subscriptions.begin_device_auth(
            provider_id,
            user_id=admin_info["user_id"],
            project_id=admin_info["project_id"],
        )
        created = bool(result.pop("_created", False))
        response.status_code = 201 if created else 200
        response.headers.update(_NO_STORE)
        return result
    except (
        errors.ActiveRunConfigurationConflict,
        errors.ChatStorageUnavailable,
        errors.ProviderAuthAttemptConflict,
        errors.ProviderConfigurationChangedError,
        errors.ProviderNotFoundError,
        errors.ProviderSubscriptionError,
        errors.ProviderValidationError,
    ) as exc:
        raise _map_subscription_error(exc) from None


@router.post(
    "/admin/providers/{provider_id}/auth/device/{attempt_id}/poll",
    response_model=DeviceAuthStatusResponse,
)
async def poll_provider_device_auth(
    provider_id: int,
    attempt_id: str,
    response: Response,
    admin_info: dict = Depends(require_admin),
):
    try:
        result = await subscriptions.poll_device_auth(
            provider_id,
            attempt_id,
            user_id=admin_info["user_id"],
            project_id=admin_info["project_id"],
        )
        response.headers.update(_NO_STORE)
        return result
    except (
        errors.ActiveRunConfigurationConflict,
        errors.ChatStorageUnavailable,
        errors.ProviderConfigurationChangedError,
        errors.ProviderNotFoundError,
        errors.ProviderSubscriptionError,
        errors.ProviderValidationError,
    ) as exc:
        raise _map_subscription_error(exc) from None


@router.delete("/admin/providers/{provider_id}/auth/device/{attempt_id}", status_code=204)
async def cancel_provider_device_auth(
    provider_id: int,
    attempt_id: str,
    admin_info: dict = Depends(require_admin),
):
    try:
        await subscriptions.cancel_device_auth(
            provider_id,
            attempt_id,
            user_id=admin_info["user_id"],
            project_id=admin_info["project_id"],
        )
    except (
        errors.ActiveRunConfigurationConflict,
        errors.ChatStorageUnavailable,
        errors.ProviderNotFoundError,
        errors.ProviderSubscriptionError,
        errors.ProviderValidationError,
    ) as exc:
        raise _map_subscription_error(exc) from None
    return Response(status_code=204, headers=_NO_STORE)


@router.put("/admin/providers/{provider_id}/auth/token", response_model=ProviderResponse)
async def set_provider_subscription_token(
    provider_id: int,
    payload: SubscriptionTokenRequest,
    response: Response,
):
    try:
        result = await subscriptions.set_subscription_token(
            provider_id,
            token=payload.token.get_secret_value(),
            expires_at=payload.expires_at,
        )
        response.headers.update(_NO_STORE)
        return result
    except (
        errors.ActiveRunConfigurationConflict,
        errors.ChatStorageUnavailable,
        errors.ProviderNotFoundError,
        errors.ProviderValidationError,
    ) as exc:
        raise _map_subscription_error(exc) from None


@router.delete("/admin/providers/{provider_id}/auth", status_code=204)
async def disconnect_provider_subscription(provider_id: int):
    try:
        await subscriptions.disconnect_subscription(provider_id)
    except (
        errors.ActiveRunConfigurationConflict,
        errors.ChatStorageUnavailable,
        errors.ProviderNotFoundError,
        errors.ProviderSubscriptionError,
        errors.ProviderValidationError,
    ) as exc:
        raise _map_subscription_error(exc) from None
    return Response(status_code=204, headers=_NO_STORE)


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


class AvailableModelCandidate(BaseModel):
    """Live provider IDs with reviewed native metadata, or unannotated compatible IDs."""

    id: str
    display_name: str | None = None
    purpose: Literal["chat", "non_chat", "unknown"] = "unknown"
    generation_methods: list[str] = Field(default_factory=list)
    input_token_limit: int | None = None
    output_token_limit: int | None = None
    # Classified from the reviewed catalog or a definitive provider method.
    model_kind: ModelKind | None = None
    provider_family: Literal["native", "compatible"] = "native"
    requires_manual_pricing: bool = False
    status: str | None = None
    token_pricing: dict | None = None
    media_pricing: dict | None = None
    capabilities: dict | None = None
    source_urls: list[str] = Field(default_factory=list)
    verified_at: str | None = None
    catalog_version: str | None = None
    catalog_entry: dict | None = None

    model_config = {"protected_namespaces": ()}


class AvailableModelsError(BaseModel):
    code: str
    message: str
    retryable: bool


class AvailableModelsResponse(BaseModel):
    provider_id: int
    fetched_at: str
    source: Literal["api", "catalog", "none"]
    live_status: Literal["success", "empty", "unsupported", "error"]
    complete: bool
    error: AvailableModelsError | None = None
    models: list[str]
    candidates: list[AvailableModelCandidate]
    provider_family: Literal["native", "compatible"] = "native"
    requires_manual_pricing: bool = False
    catalog_version: str | None = None

    model_config = {"protected_namespaces": ()}


@router.get("/admin/providers/{provider_id}/available-models", response_model=AvailableModelsResponse)
async def available_models(provider_id: int, response: Response):
    """List live provider IDs or the bundled native subscription catalog for administrator review."""
    response.headers.update(_NO_STORE)
    try:
        return await model_discovery.discover_models(provider_id)
    except errors.ProviderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc), headers=_NO_STORE) from exc
    except errors.ChatStorageUnavailable as exc:
        mapped = _map_storage(exc)
        mapped.headers = _NO_STORE
        raise mapped from exc


# ---------------------------------------------------------------------------
# 모델 엔드포인트
# ---------------------------------------------------------------------------
@router.get("/admin/models", response_model=list[ModelResponse])
async def list_models(active_only: bool = False):
    try:
        return await repository.list_models(active_only=active_only)
    except errors.ChatStorageUnavailable as exc:
        raise _map_storage(exc) from exc


@router.get("/admin/models/native-catalog", response_model=NativeCatalogResponse, response_model_exclude_unset=True)
async def get_native_catalog(provider_type: Literal["openai", "anthropic", "gemini", "perplexity"] | None = None):
    """Return the bundled, reviewed official catalog without network or database I/O."""
    return catalog_response(provider_type=provider_type)


@router.get("/admin/title-model")
async def get_title_model():
    """Configured title/compaction model and whether domain-paid compaction can use it.

    Title and memory jobs stay system-funded. Only in-run compaction is domain-paid; when the
    title model is not domain-billing ready, domain-paid runs compact with their own chat model.
    """
    try:
        route = await routing.resolve_title_model()
    except errors.ChatStorageUnavailable as exc:
        raise _map_storage(exc) from exc
    if route is None:
        return {"model_id": None, "model_name": None, "domain_billing": None,
                "domain_compaction_route": "execution_model"}
    readiness = domain_billing_readiness(route.get("capabilities") or {}, frozen_token_pricing(route))
    return {
        "model_id": route["model_id"],
        "model_name": route["model_name"],
        "domain_billing": readiness,
        "domain_compaction_route": "title_model" if readiness["ready"] else "execution_model",
    }


@router.put("/admin/title-model", status_code=204)
async def set_title_model(payload: TitleModelRequest):
    """대화 제목 자동 요약에 쓸 모델 1개를 지정(또는 해제). 요약 호출 비용은 시스템 부담."""
    try:
        await repository.set_title_model(payload.model_id)
    except errors.ProviderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except errors.ProviderValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except errors.ChatStorageUnavailable as exc:
        raise _map_storage(exc) from exc


@router.put("/admin/memory-model", status_code=204)
async def set_memory_model(payload: MemoryModelRequest):
    """채팅 후 사용자 메모리 자동 추출에 쓸 초소형 모델 1개 지정(또는 해제). 추출 비용은 시스템 부담."""
    try:
        await repository.set_memory_model(payload.model_id)
    except errors.ProviderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except errors.ProviderValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except errors.ChatStorageUnavailable as exc:
        raise _map_storage(exc) from exc


@router.post("/admin/models", response_model=ModelResponse, status_code=201)
async def create_model(payload: ModelCreateRequest):
    try:
        return await repository.create_model(**payload.model_dump())
    except errors.ImagePricingNotReady as exc:
        raise HTTPException(status_code=409, detail=exc.detail()) from exc
    except errors.ProviderValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except errors.ChatStorageUnavailable as exc:
        raise _map_storage(exc) from exc


@router.post("/admin/models/reorder", status_code=204)
async def reorder_models(payload: ModelReorderRequest):
    try:
        await repository.reorder_models(**payload.model_dump())
    except errors.ProviderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc), headers=_NO_STORE) from exc
    except (errors.ModelOrderConflictError, errors.ActiveRunConfigurationConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc), headers=_NO_STORE) from exc
    except errors.ProviderValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc), headers=_NO_STORE) from exc
    except errors.ChatStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc), headers=_NO_STORE) from exc
    return Response(status_code=204, headers=_NO_STORE)


@router.patch("/admin/models/{model_id}", response_model=ModelResponse)
async def update_model(model_id: int, payload: ModelUpdateRequest):
    try:
        return await repository.update_model(model_id, payload.model_dump(exclude_unset=True))
    except errors.ProviderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except errors.ActiveRunConfigurationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except errors.ImagePricingNotReady as exc:
        raise HTTPException(status_code=409, detail=exc.detail()) from exc
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


class ProviderAccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    is_active: bool = True
    priority: int = Field(default=0, strict=True, ge=0, le=2147483647)
    weight: int = Field(default=1, strict=True, ge=1, le=2147483647)
    allowed_model_ids: list[int] | None = None
    allowed_capabilities: list[str] | None = None
    quota_group: str | None = Field(default=None, max_length=100)
    model_config = {"extra": "forbid"}


class ProviderAccountUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    is_active: bool | None = None
    priority: int | None = Field(default=None, strict=True, ge=0, le=2147483647)
    weight: int | None = Field(default=None, strict=True, ge=1, le=2147483647)
    allowed_model_ids: list[int] | None = None
    allowed_capabilities: list[str] | None = None
    quota_group: str | None = Field(default=None, max_length=100)
    model_config = {"extra": "forbid"}


class ProviderCredentialCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    api_key: SecretStr | None = None
    api_key_env: str | None = Field(default=None, max_length=128)
    is_active: bool = True
    priority: int = Field(default=0, strict=True, ge=0, le=2147483647)
    weight: int = Field(default=1, strict=True, ge=1, le=2147483647)
    allowed_model_ids: list[int] | None = None
    allowed_capabilities: list[str] | None = None
    expires_at: datetime | None = None
    cooldown_until: datetime | None = None
    model_config = {"extra": "forbid"}


class ProviderCredentialUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    api_key: SecretStr | None = None
    api_key_env: str | None = Field(default=None, max_length=128)
    is_active: bool | None = None
    priority: int | None = Field(default=None, strict=True, ge=0, le=2147483647)
    weight: int | None = Field(default=None, strict=True, ge=1, le=2147483647)
    allowed_model_ids: list[int] | None = None
    allowed_capabilities: list[str] | None = None
    expires_at: datetime | None = None
    cooldown_until: datetime | None = None
    model_config = {"extra": "forbid"}


class ProviderCredentialResponse(BaseModel):
    id: int
    account_id: int
    name: str
    created_at: datetime = Field(description="UTC credential creation time")
    auth_mode: Literal["api_key", "chatgpt_device", "anthropic_subscription"]
    generation: int
    is_active: bool
    priority: int
    weight: int
    api_key_configured: bool
    api_key_source: str | None
    api_key_env: str | None
    subscription_status: str
    subscription_expires_at: datetime | None
    allowed_model_ids: list[int] | None
    allowed_capabilities: list[str] | None
    expires_at: datetime | None = None
    cooldown_until: datetime | None = None


class ProviderAccountResponse(ProviderAccountCreate):
    id: int
    provider_id: int
    created_at: datetime = Field(description="UTC account creation time")
    credentials: list[ProviderCredentialResponse]


def _credential_patch(payload):
    patch = payload.model_dump(exclude_unset=True)
    if isinstance(patch.get("api_key"), SecretStr):
        patch["api_key"] = patch["api_key"].get_secret_value()
    return patch


async def _account_action(operation, response):
    from sqlalchemy.exc import IntegrityError, OperationalError
    try:
        result = await operation
        response.headers.update(_NO_STORE)
        return result
    except IntegrityError:
        raise HTTPException(409, "account or credential conflicts with existing configuration", headers=_NO_STORE) from None
    except OperationalError:
        raise HTTPException(503, "provider credential storage unavailable", headers=_NO_STORE) from None
    except (errors.ProviderNotFoundError, errors.ProviderValidationError, errors.ProviderSubscriptionError,
            errors.ProviderConfigurationChangedError, errors.ProviderAuthAttemptConflict, errors.ChatStorageUnavailable) as exc:
        raise _map_subscription_error(exc) from None


@router.get("/admin/providers/{provider_id}/accounts", response_model=list[ProviderAccountResponse])
async def list_provider_accounts(provider_id: int, response: Response):
    return await _account_action(accounts.list_accounts(provider_id), response)


@router.post("/admin/providers/{provider_id}/accounts", response_model=ProviderAccountResponse, status_code=201)
async def create_provider_account(provider_id: int, payload: ProviderAccountCreate, response: Response):
    return await _account_action(accounts.create_account(provider_id, payload.model_dump()), response)


@router.patch("/admin/providers/{provider_id}/accounts/{account_id}", response_model=ProviderAccountResponse)
async def update_provider_account(provider_id: int, account_id: int, payload: ProviderAccountUpdate, response: Response):
    return await _account_action(accounts.update_account(provider_id, account_id, payload.model_dump(exclude_unset=True)), response)


@router.delete("/admin/providers/{provider_id}/accounts/{account_id}", status_code=204)
async def delete_provider_account(provider_id: int, account_id: int):
    response = Response(status_code=204, headers=_NO_STORE)
    await _account_action(accounts.delete_account(provider_id, account_id), response)
    return response


class ProviderAccountUsagePeriod(BaseModel):
    kind: Literal["calendar_month"]
    start: datetime = Field(description="Inclusive UTC month start")
    end: datetime = Field(description="Exclusive UTC start of the next month")
    timezone: Literal["UTC"]


class ProviderCredentialUsage(BaseModel):
    credential_id: int | None
    requests: int
    prompt_tokens: int
    completion_tokens: int
    raw_cost: str = Field(description="All-time Lumen-allocated raw cost as a decimal string, not a provider invoice")
    month_requests: int
    month_prompt_tokens: int
    month_completion_tokens: int
    month_raw_cost: str = Field(description="Lumen-allocated raw cost in the UTC month as a decimal string")
    last_used_at: datetime | None = Field(description="Latest UTC allocation timestamp recorded at settlement")


class ProviderAccountUsageResponse(BaseModel):
    provider_id: int
    account_id: int
    period: ProviderAccountUsagePeriod
    credentials: list[ProviderCredentialUsage]


@router.get("/admin/providers/{provider_id}/accounts/{account_id}/usage", response_model=ProviderAccountUsageResponse)
async def provider_account_usage(provider_id: int, account_id: int, response: Response):
    return await _account_action(accounts.account_usage(provider_id, account_id), response)


class ProviderRelayDispatch(BaseModel):
    attempt_id: str
    endpoint: str | None
    model: str | None
    operation: Literal["chat", "responses", "messages", "count_tokens", "embeddings", "search", "image", "tts", "stt", "realtime"]
    status: Literal["dispatched", "response_received"]
    status_code: int | None
    recorded_at: datetime


class ProviderRelayUsage(BaseModel):
    ledger_id: int
    generation: int
    settled_at: datetime
    settled_model: str
    status: Literal["settled"]
    dispatches: list[ProviderRelayDispatch] | None


class ProviderRelayPage(BaseModel):
    provider_id: int
    account_id: int
    credential_id: int
    boundary: Literal["settled_usage"]
    items: list[ProviderRelayUsage]
    next_before_id: int | None


@router.get("/admin/providers/{provider_id}/accounts/{account_id}/credentials/{credential_id}/relays", response_model=ProviderRelayPage)
async def provider_credential_relays(provider_id: int, account_id: int, credential_id: int, response: Response,
                                    limit: int = Query(default=10, ge=1, le=50),
                                    before_id: int | None = Query(default=None, gt=0)):
    return await _account_action(accounts.credential_relays(provider_id, account_id, credential_id,
        limit=limit, before_id=before_id), response)


@router.post("/admin/providers/{provider_id}/accounts/{account_id}/credentials", response_model=ProviderCredentialResponse, status_code=201)
async def create_provider_credential(provider_id: int, account_id: int, payload: ProviderCredentialCreate, response: Response):
    return await _account_action(accounts.create_credential(provider_id, account_id, _credential_patch(payload)), response)


@router.patch("/admin/providers/{provider_id}/accounts/{account_id}/credentials/{credential_id}", response_model=ProviderCredentialResponse)
async def update_provider_credential(provider_id: int, account_id: int, credential_id: int, payload: ProviderCredentialUpdate, response: Response):
    return await _account_action(accounts.update_credential(provider_id, account_id, credential_id, _credential_patch(payload)), response)


@router.delete("/admin/providers/{provider_id}/accounts/{account_id}/credentials/{credential_id}", status_code=204)
async def delete_provider_credential(provider_id: int, account_id: int, credential_id: int):
    response = Response(status_code=204, headers=_NO_STORE)
    await _account_action(accounts.delete_credential(provider_id, account_id, credential_id), response)
    return response


@router.put("/admin/providers/{provider_id}/accounts/{account_id}/credentials/{credential_id}/auth/token", response_model=ProviderCredentialResponse)
async def set_credential_subscription_token(provider_id: int, account_id: int, credential_id: int, payload: SubscriptionTokenRequest, response: Response):
    return await _account_action(subscriptions.set_subscription_token(provider_id, account_id=account_id,
        credential_id=credential_id, token=payload.token.get_secret_value(), expires_at=payload.expires_at), response)


@router.delete("/admin/providers/{provider_id}/accounts/{account_id}/credentials/{credential_id}/auth", status_code=204)
async def disconnect_credential_subscription(provider_id: int, account_id: int, credential_id: int):
    response = Response(status_code=204, headers=_NO_STORE)
    await _account_action(subscriptions.disconnect_subscription(provider_id, account_id=account_id, credential_id=credential_id), response)
    return response


@router.post("/admin/providers/{provider_id}/accounts/{account_id}/credentials/{credential_id}/auth/device", response_model=DeviceAuthStartResponse, status_code=201)
async def begin_credential_device_auth(provider_id: int, account_id: int, credential_id: int, response: Response, admin_info: dict = Depends(require_admin)):
    result = await _account_action(subscriptions.begin_device_auth(provider_id, account_id=account_id,
        credential_id=credential_id, user_id=admin_info["user_id"], project_id=admin_info["project_id"]), response)
    response.status_code = 201 if result.pop("_created", False) else 200
    return result


@router.post("/admin/providers/{provider_id}/accounts/{account_id}/credentials/{credential_id}/auth/device/{attempt_id}/poll", response_model=DeviceAuthStatusResponse)
async def poll_credential_device_auth(provider_id: int, account_id: int, credential_id: int, attempt_id: str, response: Response, admin_info: dict = Depends(require_admin)):
    return await _account_action(subscriptions.poll_device_auth(provider_id, attempt_id, account_id=account_id,
        credential_id=credential_id, user_id=admin_info["user_id"], project_id=admin_info["project_id"]), response)


@router.delete("/admin/providers/{provider_id}/accounts/{account_id}/credentials/{credential_id}/auth/device/{attempt_id}", status_code=204)
async def cancel_credential_device_auth(provider_id: int, account_id: int, credential_id: int, attempt_id: str, admin_info: dict = Depends(require_admin)):
    response = Response(status_code=204, headers=_NO_STORE)
    await _account_action(subscriptions.cancel_device_auth(provider_id, attempt_id, account_id=account_id,
        credential_id=credential_id, user_id=admin_info["user_id"], project_id=admin_info["project_id"]), response)
    return response


class ProviderProbeRequest(BaseModel):
    model_id: int = Field(strict=True, gt=0)
    generation: int = Field(strict=True, ge=0)
    model_config = {"extra": "forbid"}


class ProviderProbeResponse(BaseModel):
    status: Literal["connected", "failed"]
    reason: Literal["auth_rejected", "timeout", "invalid_response", "provider_error"] | None
    mode: Literal["structured", "plain"]
    elapsed_ms: int
    model_id: int
    generation: int
    checked_at: datetime


@router.post("/admin/providers/{provider_id}/accounts/{account_id}/credentials/{credential_id}/probe", response_model=ProviderProbeResponse)
async def probe_provider_credential(provider_id: int, account_id: int, credential_id: int,
                                    payload: ProviderProbeRequest, response: Response,
                                    admin_info: dict = Depends(require_admin)):
    from lumen.services.providers.probe import test_connection

    return await _account_action(test_connection(provider_id, account_id, credential_id,
        model_id=payload.model_id, generation=payload.generation, principal=admin_info), response)
