"""Synchronous, stateless text embeddings with token billing."""

from __future__ import annotations

import math
import uuid
from decimal import Decimal
from typing import Any

from lumen.services import credit, provider_client, provider_metadata
from lumen.services.api_key_store import ApiKeyAuthorityUnavailable, ApiKeyForbidden
from lumen.services.inference_authority import authorize_new_io
from lumen.services.providers import errors, routing
from lumen.services.usage_breakdown import UsageBreakdown


class EmbeddingError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(message)


async def resolve(model: str, *, provider: str | None = None, provider_type: str | None = None) -> dict:
    try:
        route = await routing.resolve_api_model(
            model, provider=provider, provider_type=provider_type, model_kind="embedding",
            required_capabilities=("embeddings",),
        )
    except errors.AmbiguousModelRouteError as exc:
        raise EmbeddingError(409, "model_route_ambiguous") from exc
    except errors.ChatStorageUnavailable as exc:
        raise EmbeddingError(503, "embedding service unavailable") from exc
    if route is None:
        raise EmbeddingError(404, f"embedding model not found: {model}")
    return route


def _values(response: Any) -> list[dict]:
    data = getattr(response, "data", None)
    if not isinstance(data, list) or not data:
        raise EmbeddingError(502, "invalid embedding response")
    result = []
    for index, item in enumerate(data):
        vector = item.get("embedding") if isinstance(item, dict) else getattr(item, "embedding", None)
        if not isinstance(vector, (list, tuple)) or not vector:
            raise EmbeddingError(502, "invalid embedding response")
        values = [float(value) for value in vector]
        if not all(math.isfinite(value) for value in values):
            raise EmbeddingError(502, "invalid embedding response")
        result.append({"index": index, "embedding": values})
    return result


def _limits(route: dict, inputs: list[str], dimensions: int | None) -> tuple[int, bool]:
    metadata = provider_metadata.catalog_model_metadata(route["model_name"], route.get("provider_type") or "")
    model = route["model_name"].lower()
    provider = route.get("provider_type")
    max_dimensions = metadata.get("output_vector_size")
    supports_dimensions = (provider == "openai" and model.startswith("text-embedding-3-")) or (
        provider == "gemini" and "embedding" in model
    )
    if max_dimensions is None and supports_dimensions:
        max_dimensions = 3072 if provider == "gemini" or "large" in model else 1536
    if dimensions is not None and (not supports_dimensions or dimensions > max_dimensions):
        raise EmbeddingError(400, "dimensions is unsupported or exceeds this model's output dimensions")
    max_tokens = metadata.get("max_input_tokens") or (8191 if provider == "openai" else 2048)
    counts = [provider_metadata.count_tokens(route["model_name"], text=value) for value in inputs]
    if any(count > max_tokens for count in counts):
        raise EmbeddingError(400, "input exceeds the model token limit")
    if provider == "openai" and sum(counts) > 300000:
        raise EmbeddingError(400, "input exceeds the request token limit")
    expected = dimensions or max_dimensions
    return expected or 0, supports_dimensions


async def embed(
    *, route: dict, inputs: list[str], user_id: str, project_id: str, api_key_id: int | None,
    required_scopes: tuple[str, ...], dimensions: int | None = None,
    task_type: str | None = None, title: str | None = None,
) -> dict:
    expected_dimensions, _ = _limits(route, inputs, dimensions)
    if route.get("input_price_per_token") is None:
        raise EmbeddingError(422, "embedding input pricing unavailable")
    try:
        await credit.precheck(user_id, project_id, api_key_id=api_key_id)
    except credit.QuotaExceeded as exc:
        raise EmbeddingError(429, str(exc)) from exc
    except credit.ChatStorageUnavailable as exc:
        raise EmbeddingError(503, "embedding service unavailable") from exc
    try:
        await authorize_new_io(
            user_id=user_id, project_id=project_id, api_key_id=api_key_id,
            required_scopes=required_scopes,
        )
        response = await provider_client.aembedding(
            model=route["model_name"], input=inputs,
            api_base=route.get("api_base"), api_key=route.get("api_key"),
            custom_llm_provider=route.get("provider_type"), provider_auth=route.get("provider_auth"),
            dimensions=dimensions,
            task_type=task_type if route.get("provider_type") == "gemini" else None,
            title=title if route.get("provider_type") == "gemini" else None,
        )
        vectors = _values(response)
    except EmbeddingError:
        raise
    except ApiKeyForbidden as exc:
        raise EmbeddingError(403, "inference authority revoked") from exc
    except ApiKeyAuthorityUnavailable as exc:
        raise EmbeddingError(503, "inference authority unavailable") from exc
    except errors.ProviderSubscriptionError as exc:
        raise EmbeddingError(exc.status_code, "embedding protocol unsupported") from None
    except Exception as exc:
        raise EmbeddingError(502, "upstream embedding error") from exc
    if len(vectors) != len(inputs) or any(
        expected_dimensions and len(item["embedding"]) != expected_dimensions for item in vectors
    ):
        raise EmbeddingError(502, "embedding response dimensions do not match the request")

    usage = getattr(response, "usage", None)
    prompt_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else (
        getattr(usage, "prompt_tokens", None) if usage is not None else None
    )
    estimated = type(prompt_tokens) is not int or prompt_tokens <= 0
    if estimated:
        prompt_tokens = sum(provider_metadata.count_tokens(route["model_name"], text=text) for text in inputs)
    breakdown = UsageBreakdown.from_totals(prompt_tokens, 0)
    try:
        cost = provider_metadata.cost_from_usage(
            route["model_name"], prompt_tokens, 0,
            input_price_per_token=route.get("input_price_per_token"),
            output_price_per_token=Decimal(0), price_source=route.get("price_source"),
            provider_type=route.get("provider_type"), api_base=route.get("api_base"), breakdown=breakdown,
            allow_catalog_prices=True,
        )
        credited = await credit.apply_usage(
            event_id=str(uuid.uuid4()), user_id=user_id, project_id=project_id,
            model_name=route["model_name"], provider=route.get("provider_name"),
            prompt_tokens=prompt_tokens, completion_tokens=0, usage_cost=cost,
            margin_multiplier=route["margin_multiplier"], source="api", api_key_id=api_key_id,
            breakdown=breakdown, execution_route=route,
        )
    except Exception as exc:
        raise EmbeddingError(503, "embedding usage settlement unavailable") from exc
    return {
        "model": route["api_model_name"], "embeddings": vectors,
        "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens, "estimated": estimated},
        "credited_cost": float(credited),
    }
