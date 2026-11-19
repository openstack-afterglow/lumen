"""Synchronous, stateless text embeddings with token billing."""

from __future__ import annotations

import math
import uuid
from decimal import Decimal
from typing import Any

import anyio

from lumen.config import get_settings
from lumen.models.billing import Payer
from lumen.services import credit, provider_client, provider_metadata, stateless_credit
from lumen.services.api_key_store import ApiKeyAuthorityUnavailable, ApiKeyForbidden
from lumen.services.inference_authority import authorize_new_io
from lumen.services.provider_errors import ProviderError
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


def embedding_metadata(route: dict) -> dict:
    """Use resolved registry metadata; only bare native routes consult the catalog."""
    caps = route.get("capabilities") or {}
    metadata = {}
    if "embedding" in caps:
        embedding = caps["embedding"]
    elif provider_metadata.direct_provider_route(route.get("provider_type"), route.get("api_base")):
        metadata = provider_metadata.catalog_model_metadata(
            route["model_name"], route.get("provider_type") or "", route.get("api_base"),
        )
        embedding = metadata.get("embedding")
    else:
        embedding = None
    embedding = embedding if isinstance(embedding, dict) else {}
    return {
        "default_dimensions": embedding.get("default_dimensions"),
        "supports_dimensions": embedding.get("supports_dimensions") is True,
        "min_dimensions": embedding.get("min_dimensions"),
        "max_dimensions": embedding.get("max_dimensions"),
        "allowed_dimensions": embedding.get("allowed_dimensions"),
        "max_input_tokens": caps.get("context_limit") or metadata.get("max_input_tokens"),
    }


def _limits(route: dict, inputs: list[str], dimensions: int | None) -> tuple[int, bool]:
    metadata = embedding_metadata(route)
    supports_dimensions = metadata["supports_dimensions"]
    minimum, maximum = metadata["min_dimensions"], metadata["max_dimensions"]
    allowed = metadata["allowed_dimensions"]
    if dimensions is not None and (
        type(dimensions) is not int or dimensions < 1 or not supports_dimensions
        or minimum is not None and dimensions < minimum
        or maximum is not None and dimensions > maximum
        or allowed is not None and dimensions not in allowed
    ):
        raise EmbeddingError(400, "dimensions is unsupported or exceeds this model's output dimensions")
    max_tokens = metadata["max_input_tokens"]
    counts = [provider_metadata.count_tokens(route["model_name"], text=value) for value in inputs]
    if max_tokens is not None and any(count > max_tokens for count in counts):
        raise EmbeddingError(400, "input exceeds the model token limit")
    if (
        route.get("provider_type") == "openai"
        and provider_metadata.direct_provider_route("openai", route.get("api_base"))
        and sum(counts) > 300000
    ):
        raise EmbeddingError(400, "input exceeds the request token limit")
    expected = dimensions if dimensions is not None else metadata["default_dimensions"]
    return expected or 0, supports_dimensions


async def embed(
    *, route: dict, inputs: list[str], user_id: str, project_id: str, api_key_id: int | None,
    required_scopes: tuple[str, ...], dimensions: int | None = None,
    task_type: str | None = None, title: str | None = None, payer: Payer | None = None,
) -> dict:
    expected_dimensions, _ = _limits(route, inputs, dimensions)
    if route.get("input_price_per_token") is None:
        raise EmbeddingError(422, "embedding input pricing unavailable")
    domain_billed = payer is not None and payer.domain_id is not None
    bounded = payer is not None
    event_id = str(uuid.uuid4())
    held = dispatched = False
    conversion = Decimal(str(get_settings().chat_credit_per_usd))
    try:
        if not bounded:
            await credit.precheck(user_id, project_id, api_key_id=api_key_id, payer=payer)
        await authorize_new_io(
            user_id=user_id, project_id=project_id, api_key_id=api_key_id, required_scopes=required_scopes,
        )
        if bounded:
            max_tokens = embedding_metadata(route)["max_input_tokens"]
            if type(max_tokens) is not int or max_tokens <= 0:
                raise EmbeddingError(422, "embedding input bound unavailable")
            max_tokens *= len(inputs)
            if route.get("provider_type") == "openai" and provider_metadata.direct_provider_route(
                "openai", route.get("api_base"),
            ):
                max_tokens = min(max_tokens, 300000)
            bound = credit.call_credit_bound(
                {"input_price_per_token": route["input_price_per_token"], "output_price_per_token": 0,
                 "chat_credit_per_usd": conversion, "margin_multiplier": route["margin_multiplier"]},
                input_tokens=max_tokens, output_tokens=0,
            )
            await stateless_credit.reserve(event_id=event_id, user_id=user_id, project_id=project_id,
                                           api_key_id=api_key_id, payer=payer, bound=bound)
            held = True

        dispatched = True
        response = await provider_client.aembedding(
            model=route["model_name"], input=inputs,
            api_base=route.get("api_base"), api_key=route.get("api_key"),
            custom_llm_provider=route.get("provider_type"), provider_auth=route.get("provider_auth"),
            dimensions=dimensions,
            task_type=task_type if route.get("provider_type") == "gemini" else None,
            title=title if route.get("provider_type") == "gemini" else None,
        )
        vectors = _values(response)
        if len(vectors) != len(inputs) or any(
            expected_dimensions and len(item["embedding"]) != expected_dimensions for item in vectors
        ):
            raise EmbeddingError(502, "embedding response dimensions do not match the request")

        usage = getattr(response, "usage", None)
        prompt_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else (
            getattr(usage, "prompt_tokens", None) if usage is not None else None
        )
        estimated = type(prompt_tokens) is not int or prompt_tokens <= 0
        if bounded and (estimated or prompt_tokens > max_tokens):
            raise EmbeddingError(502, "provider_result_unknown")
        if estimated:
            prompt_tokens = sum(provider_metadata.count_tokens(route["model_name"], text=text) for text in inputs)
        breakdown = UsageBreakdown.from_totals(prompt_tokens, 0)
        try:
            cost = provider_metadata.cost_from_usage(
                route["model_name"], prompt_tokens, 0,
                input_price_per_token=route.get("input_price_per_token"),
                output_price_per_token=Decimal(0), price_source=route.get("price_source"),
                provider_type=route.get("provider_type"), api_base=route.get("api_base"), breakdown=breakdown,
                allow_catalog_prices=not bounded,
            )
            settle = stateless_credit.settle if held else credit.apply_usage
            with anyio.CancelScope(shield=held):
                credited = await settle(
                    event_id=event_id, user_id=user_id, project_id=project_id, payer=payer,
                    model_name=route["model_name"], provider=route.get("provider_name"),
                    prompt_tokens=prompt_tokens, completion_tokens=0, usage_cost=cost,
                    margin_multiplier=route["margin_multiplier"], credit_per_usd=conversion,
                    source="api" if api_key_id is not None or not domain_billed else "web", api_key_id=api_key_id,
                    breakdown=breakdown, execution_route=route,
                )
                held = False
        except stateless_credit.ChargeExceedsReservation as exc:
            raise EmbeddingError(502, "provider_result_unknown") from exc
        except Exception as exc:
            raise EmbeddingError(503, "embedding usage settlement unavailable") from exc
    except credit.QuotaExceeded as exc:
        raise EmbeddingError(429, str(exc)) from exc
    except credit.ChatStorageUnavailable as exc:
        raise EmbeddingError(503, "embedding service unavailable") from exc
    except EmbeddingError:
        raise
    except ApiKeyForbidden as exc:
        raise EmbeddingError(403, "inference authority revoked") from exc
    except ApiKeyAuthorityUnavailable as exc:
        raise EmbeddingError(503, "inference authority unavailable") from exc
    except errors.ProviderSubscriptionError as exc:
        dispatched = False
        raise EmbeddingError(exc.status_code, "embedding protocol unsupported") from None
    except ProviderError as exc:
        dispatched = not exc.known_rejection
        raise EmbeddingError(502, "upstream embedding error") from exc
    except Exception as exc:
        raise EmbeddingError(502, "upstream embedding error") from exc
    finally:
        if held:
            with anyio.CancelScope(shield=True):
                await stateless_credit.finish(event_id, uncertain=dispatched, payer=payer)
    return {
        "model": route["api_model_name"], "embeddings": vectors,
        "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens, "estimated": estimated},
        "credited_cost": float(credited),
    }
