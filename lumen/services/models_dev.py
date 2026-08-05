"""Read-only models.dev catalog client for administrator-reviewed price defaults."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import httpx

from lumen.cache import cached_call

SOURCE_URL = "https://models.dev/api.json"
_CACHE_KEY = "afterglow:chat:models-dev:api:v1"
_CACHE_TTL_SECONDS = 3600
_TIMEOUT_SECONDS = 10
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024


class ModelsDevCatalogError(RuntimeError):
    """The advisory catalog cannot be trusted for an import."""


@dataclass(frozen=True)
class CatalogModel:
    id: str
    name: str
    last_updated: str | None
    input_price_per_million: Decimal | None
    output_price_per_million: Decimal | None
    price_available: bool
    unsupported_price_fields: list[str]
    cost: dict[str, object] | None
    # 정규화된 능력(JSON-safe: context_limit=int, 나머지 bool/str). 관리자 import·게이팅에 사용.
    capabilities: dict[str, object] | None


@dataclass(frozen=True)
class CatalogProvider:
    id: str
    name: str
    models: dict[str, CatalogModel]


@dataclass(frozen=True)
class ModelsDevCatalog:
    source_url: str
    fetched_at: str
    providers: dict[str, CatalogProvider]


def _require_id(value: object, kind: str) -> str:
    if not isinstance(value, str) or not value:
        raise ModelsDevCatalogError(f"models.dev {kind} id 형식이 올바르지 않습니다")
    return value


def _price(value: object, field: str) -> Decimal:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise ModelsDevCatalogError(f"models.dev {field} 가격 형식이 올바르지 않습니다") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ModelsDevCatalogError(f"models.dev {field} 가격은 유한한 0 이상의 숫자여야 합니다")
    return parsed


def _str_list(value: object) -> list[str]:
    return [str(x) for x in value] if isinstance(value, list) else []


def _model_capabilities(model_data: dict[str, object]) -> dict[str, object]:
    """models.dev 모델 객체 → 정규화된 능력 dict(JSON-safe). 누락 필드는 방어적 기본값.

    필드: vision(=modalities.input에 image), reasoning, tool_call, attachment,
    modalities{input,output}, reasoning_options, context_limit(int).
    """
    modalities = model_data.get("modalities")
    inp = _str_list(modalities.get("input")) if isinstance(modalities, dict) else []
    out = _str_list(modalities.get("output")) if isinstance(modalities, dict) else []
    limit = model_data.get("limit")
    ctx = limit.get("context") if isinstance(limit, dict) else None
    try:
        context_limit = int(ctx) if ctx is not None else None
    except (TypeError, ValueError):
        context_limit = None
    # reasoning_options: [{"type":"effort","values":["low","medium","high"]}] — values를 문자열로 고정.
    raw_options = model_data.get("reasoning_options")
    reasoning_options: list[dict[str, object]] = []
    if isinstance(raw_options, list):
        for opt in raw_options:
            if isinstance(opt, dict):
                reasoning_options.append({"type": str(opt.get("type") or ""), "values": _str_list(opt.get("values"))})
    return {
        "vision": "image" in inp,
        "reasoning": bool(model_data.get("reasoning")),
        "tool_call": bool(model_data.get("tool_call")),
        "attachment": bool(model_data.get("attachment")),
        "modalities": {"input": inp, "output": out},
        "reasoning_options": reasoning_options,
        "context_limit": context_limit,
    }


def _unsupported_price_fields(cost: dict[str, object], model: dict[str, object]) -> list[str]:
    fields = [f"cost.{key}" for key in cost if key not in {"input", "output"}]
    experimental = model.get("experimental")
    if isinstance(experimental, dict):
        modes = experimental.get("modes")
        if isinstance(modes, dict):
            for mode_id, mode in modes.items():
                if isinstance(mode, dict) and "cost" in mode:
                    fields.append(f"experimental.modes.{mode_id}.cost")
    return sorted(fields)


def parse_catalog(body: str, fetched_at: str) -> ModelsDevCatalog:
    try:
        payload = json.loads(
            body,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ModelsDevCatalogError(f"허용되지 않은 JSON 값: {value}")
            ),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, ModelsDevCatalogError):
            raise
        raise ModelsDevCatalogError("models.dev 응답 JSON을 해석할 수 없습니다") from exc
    if not isinstance(payload, dict):
        raise ModelsDevCatalogError("models.dev providers schema가 올바르지 않습니다")
    # models.dev publishes a top-level provider map. Accept the historical wrapper
    # only so cached payloads from an earlier client version remain readable.
    raw_providers = payload.get("providers") if isinstance(payload.get("providers"), dict) else payload
    if not isinstance(raw_providers, dict):
        raise ModelsDevCatalogError("models.dev providers schema가 올바르지 않습니다")

    providers: dict[str, CatalogProvider] = {}
    for provider_id, provider_data in raw_providers.items():
        provider_key = _require_id(provider_id, "provider")
        if not isinstance(provider_data, dict):
            raise ModelsDevCatalogError(f"models.dev provider {provider_key} schema가 올바르지 않습니다")
        if "id" in provider_data and _require_id(provider_data["id"], "provider") != provider_key:
            raise ModelsDevCatalogError(f"models.dev provider key/id가 일치하지 않습니다: {provider_key}")
        raw_models = provider_data.get("models")
        if not isinstance(raw_models, dict):
            raise ModelsDevCatalogError(f"models.dev provider {provider_key} models schema가 올바르지 않습니다")
        models: dict[str, CatalogModel] = {}
        for model_id, model_data in raw_models.items():
            model_key = _require_id(model_id, "model")
            if not isinstance(model_data, dict):
                raise ModelsDevCatalogError(f"models.dev model {model_key} schema가 올바르지 않습니다")
            if "id" in model_data and _require_id(model_data["id"], "model") != model_key:
                raise ModelsDevCatalogError(f"models.dev model key/id가 일치하지 않습니다: {model_key}")
            cost = model_data.get("cost")
            if cost is not None and not isinstance(cost, dict):
                raise ModelsDevCatalogError(f"models.dev model {model_key} cost schema가 올바르지 않습니다")
            price_available = isinstance(cost, dict) and "input" in cost and "output" in cost
            input_price = _price(cost["input"], "input") if price_available else None
            output_price = _price(cost["output"], "output") if price_available else None
            models[model_key] = CatalogModel(
                id=model_key,
                name=str(model_data.get("name") or model_key),
                last_updated=str(model_data["last_updated"]) if model_data.get("last_updated") is not None else None,
                input_price_per_million=input_price,
                output_price_per_million=output_price,
                price_available=price_available,
                unsupported_price_fields=_unsupported_price_fields(cost or {}, model_data),
                cost=cost,
                capabilities=_model_capabilities(model_data),
            )
        providers[provider_key] = CatalogProvider(
            id=provider_key,
            name=str(provider_data.get("name") or provider_key),
            models=models,
        )
    return ModelsDevCatalog(source_url=SOURCE_URL, fetched_at=fetched_at, providers=providers)


async def _download() -> dict[str, str]:
    timeout = httpx.Timeout(_TIMEOUT_SECONDS)
    try:
        async with (
            httpx.AsyncClient(timeout=timeout) as client,
            client.stream("GET", SOURCE_URL, headers={"Accept": "application/json"}) as response,
        ):
            if response.status_code < 200 or response.status_code >= 300:
                raise ModelsDevCatalogError(f"models.dev 응답 상태가 올바르지 않습니다: {response.status_code}")
            content_type = response.headers.get("content-type", "")
            if "application/json" not in content_type.lower():
                raise ModelsDevCatalogError("models.dev 응답 content-type이 JSON이 아닙니다")
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > _MAX_RESPONSE_BYTES:
                    raise ModelsDevCatalogError("models.dev 응답이 최대 크기를 초과했습니다")
    except httpx.HTTPError as exc:
        raise ModelsDevCatalogError("models.dev catalog을 가져올 수 없습니다") from exc
    try:
        body = chunks.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ModelsDevCatalogError("models.dev 응답 인코딩이 올바르지 않습니다") from exc
    return {"body": body, "fetched_at": datetime.now(UTC).isoformat()}


async def get_catalog(*, refresh: bool = False) -> ModelsDevCatalog:
    cached = await cached_call(_CACHE_KEY, _CACHE_TTL_SECONDS, _download, refresh=refresh)
    if (
        not isinstance(cached, dict)
        or not isinstance(cached.get("body"), str)
        or not isinstance(cached.get("fetched_at"), str)
    ):
        raise ModelsDevCatalogError("models.dev cache schema가 올바르지 않습니다")
    return parse_catalog(cached["body"], cached["fetched_at"])


def _normalized_provider_identifier(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def matching_provider_ids(catalog: ModelsDevCatalog, registered_provider: dict[str, object]) -> set[str]:
    """Return catalog IDs that safely describe one registered local provider."""
    matched_ids: set[str] = set()
    mapped_id = registered_provider.get("models_dev_provider_id")
    if isinstance(mapped_id, str) and mapped_id in catalog.providers:
        matched_ids.add(mapped_id)

    local_identifiers = {
        _normalized_provider_identifier(registered_provider.get(field)) for field in ("provider_type", "name")
    }
    local_identifiers.discard("")
    if not local_identifiers:
        return matched_ids

    for provider in catalog.providers.values():
        catalog_identifiers = {
            _normalized_provider_identifier(provider.id),
            _normalized_provider_identifier(provider.name),
        }
        if local_identifiers & catalog_identifiers:
            matched_ids.add(provider.id)
    return matched_ids


def provider_list(
    catalog: ModelsDevCatalog,
    provider_ids: set[str] | None = None,
    preferred_provider_ids: set[str] | None = None,
) -> list[dict[str, object]]:
    """Return a deterministic catalog-provider listing, optionally narrowed by ID."""
    preferred_provider_ids = preferred_provider_ids or set()
    providers = (
        provider for provider in catalog.providers.values() if provider_ids is None or provider.id in provider_ids
    )
    return [
        {"id": provider.id, "name": provider.name, "model_count": len(provider.models)}
        for provider in sorted(
            providers,
            key=lambda provider: (
                provider.id not in preferred_provider_ids,
                provider.name.casefold(),
                provider.id,
            ),
        )
    ]


def registered_provider_list(
    catalog: ModelsDevCatalog,
    registered_providers: list[dict[str, object]],
    current_provider_id: int,
) -> list[dict[str, object]]:
    """List catalog providers matched to registered services, current service first."""
    matched_ids: set[str] = set()
    preferred_ids: set[str] = set()
    for provider in registered_providers:
        provider_matches = matching_provider_ids(catalog, provider)
        matched_ids.update(provider_matches)
        if provider.get("id") == current_provider_id:
            preferred_ids.update(provider_matches)
    return provider_list(catalog, matched_ids, preferred_ids)


def provider_detail(catalog: ModelsDevCatalog, provider_id: str) -> dict[str, object] | None:
    provider = catalog.providers.get(provider_id)
    if provider is None:
        return None
    return {
        "id": provider.id,
        "name": provider.name,
        "models": [
            {
                "id": model.id,
                "name": model.name,
                "last_updated": model.last_updated,
                "input_price_per_million": (
                    format(model.input_price_per_million, "f") if model.input_price_per_million is not None else None
                ),
                "output_price_per_million": (
                    format(model.output_price_per_million, "f") if model.output_price_per_million is not None else None
                ),
                "price_available": model.price_available,
                "unsupported_price_fields": model.unsupported_price_fields,
                "capabilities": model.capabilities,
            }
            for model in provider.models.values()
        ],
    }
